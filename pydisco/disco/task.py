import hashlib, os, random, re, subprocess, sys

from itertools import chain
from types import FunctionType

from disco import func
from disco.core import Disco, JobDict
from disco.error import DiscoError
from disco.events import Message, OutputURL, OOBData, TaskFailed
from disco.fileutils import AtomicFile
from disco.fileutils import ensure_file, ensure_path, safe_update, write_files
from disco.node import external, worker
from disco.settings import DiscoSettings
from disco.util import ddfs_save, iskv, load_oob, netloc, urllist

oob_chars = re.compile(r'[^a-zA-Z_\-:0-9]')

class Task(object):
    default_paths = {
        'CHDIR_PATH':    '',
        'JOBPACK':       'params.dl',
        'REQ_FILES':     'lib',
        'EXT_MAP':       'ext.map',
        'EXT_REDUCE':    'ext.reduce',
        'MAP_OUTPUT':    'map-disco-%d-%.9d',
        'PART_OUTPUT':   'part-disco-%.9d',
        'REDUCE_DL':     'reduce-in-%d.dl',
        'REDUCE_SORTED': 'reduce-in-%d.sorted',
        'REDUCE_OUTPUT': 'reduce-disco-%d',
        'OOB_FILE':      'oob/%s',
        'MAP_INDEX':     'map-index.txt',
        'REDUCE_INDEX':  'reduce-index.txt'
    }

    def __init__(self,
                 netlocstr='',
                 id=-1,
                 inputs=None,
                 jobdict=None,
                 jobname='',
                 master=None,
                 settings=DiscoSettings()):
        self.netloc   = netloc.parse(netlocstr)
        self.id       = int(id)
        self.inputs   = inputs
        self.jobdict  = jobdict
        self.jobname  = jobname
        self.master   = master
        self.settings = settings
        self._blobs   = []

        if not jobdict:
            if netlocstr:
                self.jobdict = JobDict.unpack(open(self.jobpack),
                                              globals=worker.__dict__)
            else:
                self.jobdict = JobDict(map=func.noop)

    def __getattr__(self, key):
        if key in self.jobdict:
            return self.jobdict[key]
        task_key = '%s_%s' % (self.__class__.__name__.lower(), key)
        if task_key in self.jobdict:
            return self.jobdict[task_key]
        raise AttributeError("%s has no attribute %s" % (self, key))

    @property
    def datadir(self):
        dir = 'temp' if self.has_flag('resultfs') else 'data'
        return os.path.join(self.root, dir)

    @property
    def dataroot(self):
        return self.settings['DISCO_DATA']

    @property
    def ddfsroot(self):
        return self.settings['DDFS_ROOT']

    @property
    def flags(self):
        return self.settings['DISCO_FLAGS'].lower().split()

    @property
    def hex_key(self):
        return hashlib.md5(self.jobname).hexdigest()[:2]

    @property
    def home(self):
        return os.path.join(self.host, self.hex_key, self.jobname)

    @property
    def host(self):
        return self.netloc[0]

    @property
    def jobroot(self):
        return os.path.join(self.root, self.datadir, self.home)

    @property
    def port(self):
        return self.settings['DISCO_PORT']

    @property
    def root(self):
        return self.settings['DISCO_ROOT']

    @property
    def blobs(self):
        return self._blobs

    def add_blob(self, blob):
        self._blobs.append(blob)

    def has_flag(self, flag):
        return flag.lower() in self.flags

    def path(self, name, *args):
        path = self.default_paths[name] % args
        return os.path.join(self.jobroot, path)

    def url(self, name, *args, **kwargs):
        path = self.default_paths[name] % args
        scheme = kwargs.get('scheme', 'disco')
        return '%s://%s/disco/%s/%s' % (scheme, self.host, self.home, path)

    @property
    def map_index(self):
        return (self.path('MAP_INDEX'),
                self.url('MAP_INDEX', scheme='dir'))

    @property
    def reduce_index(self):
        return (self.path('REDUCE_INDEX'),
                self.url('REDUCE_INDEX', scheme='dir'))

    def map_output(self, partition):
        return (self.path('MAP_OUTPUT', self.id, partition),
                self.url('MAP_OUTPUT', self.id, partition))

    def partition_output(self, partition):
        return (self.path('PART_OUTPUT', partition),
                self.url('PART_OUTPUT', partition))

    def reduce_output(self):
        return (self.path('REDUCE_OUTPUT', self.id),
                self.url('REDUCE_OUTPUT', self.id))

    def oob_file(self, key):
        return self.path('OOB_FILE', key)

    @property
    def jobpack(self):
        jobpack = self.path('JOBPACK')
        ensure_path(os.path.dirname(jobpack))
        def data():
            return Disco(self.master).jobpack(self.jobname)
        ensure_file(jobpack, data=data, mode=444)
        return jobpack

    @property
    def ispartitioned(self):
        return self.jobdict['nr_reduces'] > 0

    @property
    def num_partitions(self):
        return max(1, self.jobdict['nr_reduces'])

    def put(self, key, value):
        """
        Stores an out-of-band result *value* with the key *key*. Key must be unique in
        this job. Maximum key length is 256 characters. Only characters in the set
        ``[a-zA-Z_\-:0-9]`` are allowed in the key.
        """
        if oob_chars.match(key):
            raise DiscoError("OOB key contains invalid characters (%s)" % key)
        if value is not None:
            file(self.oob_file(key), 'w').write(value)
        OOBData(key, self)

    def get(self, key, job=None):
        """
        Gets an out-of-band result assigned with the key *key*. The job name *job*
        defaults to the current job.

        Given the semantics of OOB results (see above), this means that the default
        value is only good for the reduce phase which can access results produced
        in the preceding map phase.
        """
        return load_oob(self.master, job or self.jobname, key)

    def track_status(self, iterator, message_template):
        status_interval = self.status_interval
        n = 0
        for n, item in enumerate(iterator):
            if status_interval and n % status_interval == 0:
                Message(message_template % n)
            yield item
        Message("Done: %s" % (message_template % (n + 1)))

    def connect_input(self, url):
        fd = sze = None
        for input_stream in self.input_stream:
            fd, sze, url = input_stream(fd, sze, url, self.params)
        return fd, sze, url

    def connect_output(self, part=0):
        fd = url = None
        fd_list = []
        for output_stream in self.output_stream:
            fd, url = output_stream(fd, part, url, self.params)
            fd_list.append(fd)
        return fd, url, fd_list

    def close_output(self, fd_list):
        for fd in reversed(fd_list):
            if hasattr(fd, 'close'):
                fd.close()

    @property
    def functions(self):
        for fn in chain((getattr(self, name) for name in self.jobdict.functions),
                        *(getattr(self, stack) for stack in self.jobdict.stacks)):
            if fn:
                yield fn

    def insert_globals(self, functions):
        for fn in functions:
            fn.func_globals.setdefault('Task', self)
            for module in self.required_modules:
                mod_name = module[0] if iskv(module) else module
                mod = __import__(mod_name, fromlist=[mod_name])
                fn.func_globals.setdefault(mod_name.split('.')[-1], mod)

    def run(self):
        assert self.version == '%s.%s' % sys.version_info[:2], "Python version mismatch"
        ensure_path(os.path.dirname(self.path('OOB_FILE', '')))
        os.chdir(self.path('CHDIR_PATH'))
        path = self.path('REQ_FILES')
        write_files(self.required_files, path)
        sys.path.insert(0, path)
        if self.profile:
            self._run_profile()
        else:
            self._run()

    def _run_profile(self):
        try:
            import cProfile as prof
        except ImportError:
            import profile as prof
        filename = 'profile-%s-%s' % (self.__class__.__name__, self.id)
        path     = self.path('OOB_FILE', filename)
        prof.runctx('self._run()', globals(), locals(), path)
        self.put(filename, None)

class Map(Task):
    def _run(self):
        if len(self.inputs) != 1:
            TaskFailed("Map can only handle one input. Got: %s" % ' '.join(self.inputs))

        if self.ext_map:
            external.prepare(self.map, self.params, self.path('EXT_MAP'))
            self.map = FunctionType(external.ext_map.func_code,
                                    globals=external.__dict__)
        self.insert_globals(self.functions)

        partitions = [MapOutput(self, i) for i in xrange(self.num_partitions)]
        fd, sze, url = self.connect_input(self.inputs[0])
        reader = self.reader(fd, sze, url)
        params = self.params
        self.init(reader, params)

        entries = (self.map(entry, params) for entry in reader)
        for kvs in self.track_status(entries, "%s entries mapped"):
            for k, v in kvs:
                p = self.partition(k, self.num_partitions, params)
                partitions[p].add(k, v)

        external.close_ext()

        urls = {}
        for i, partition in enumerate(partitions):
            partition.close()
            urls['%d %s' % (i, partition.url)] = True

        index, index_url = self.map_index
        safe_update(index, urls)

        if self.save and not self.reduce:
            if self.ispartitioned:
                TaskFailed("Storing partitioned outputs in DDFS is not yet supported")
            else:
                OutputURL(ddfs_save(self.blobs, self.jobname, self.master))
                Message("Results pushed to DDFS")
        else:
            OutputURL(index_url)

    @property
    def params(self):
        if self.ext_map:
            return self.ext_params or '0\n'
        return self.jobdict['params']

class MapOutput(object):
    def __init__(self, task, partition):
        self.task = task
        self.comb_buffer = {}
        self.fd, self.url, self.fd_list = task.connect_output(partition)

    def add(self, key, value):
        if self.task.combiner:
            ret = self.task.combiner(key, value,
                                     self.comb_buffer,
                                     0,
                                     self.task.params)
            if ret:
                for key, value in ret:
                    self.task.writer(self.fd, key, value, self.task.params)
        else:
            self.task.writer(self.fd, key, value, self.task.params)

    def close(self):
        if self.task.combiner:
            ret = self.task.combiner(None, None,
                                     self.comb_buffer,
                                     1,
                                     self.task.params)
            if ret:
                for key, value in ret:
                    self.task.writer(self.fd, key, value, self.task.params)
        self.task.close_output(self.fd_list)

def num_cmp(x, y):
    try:
        x = (int(x[0]), x[1])
        y = (int(y[0]), y[1])
    except ValueError:
        pass
    return cmp(x, y)

class Reduce(Task):
    def _run(self):
        red_in  = iter(ReduceReader(self))
        red_out = ReduceOutput(self)
        params  = self.params

        if self.ext_reduce:
            path = self.path('EXT_REDUCE')
            external.prepare(self.reduce, self.params, path)
            self.reduce = FunctionType(external.ext_reduce.func_code,
                                       globals=external.__dict__)
        self.insert_globals(self.functions)

        Message("Starting reduce")
        self.init(red_in, params)
        self.reduce(red_in, red_out, params)
        Message("Reduce done")

        red_out.close()
        external.close_ext()

        if self.save:
            OutputURL(ddfs_save(self.blobs, self.jobname, self.master))
            Message("Results pushed to DDFS")
        else:
            index, index_url = self.reduce_index
            safe_update(index, {'%d %s' % (self.id, red_out.url): True})
            OutputURL(index_url)

    @property
    def params(self):
        if self.ext_reduce:
            return self.ext_params or '0\n'
        return self.jobdict['params']

class ReduceOutput(object):
    def __init__(self, task):
        self.task = task
        self.fd, self.url, self.fd_list = self.task.connect_output()

    def add(self, key, value):
        self.task.writer(self.fd, key, value, self.task.params)

    def close(self):
        self.task.close_output(self.fd_list)

class ReduceReader(object):
    def __init__(self, task):
        self.task   = task
        self.inputs = [url for input in task.inputs
                       for url in urllist(input, partid=task.id)]
        random.shuffle(self.inputs)

    def __iter__(self):
        if self.task.sort:
            total_size = 0
            for input in self.inputs:
                fd, sze, url = self.task.connect_input(input)
                total_size += sze

            Message("Reduce[%d] input is %.2fMB" % (self.task.id,
                                                    total_size / 1024.0**2))

            if total_size > self.task.mem_sort_limit:
                return self.download_and_sort()
            return self.memory_sort()
        return self.multi_file_iterator(self.task.reader)

    def download_and_sort(self):
        dlname = self.task.path('REDUCE_DL', self.task.id)
        Message("Reduce will be downloaded to %s" % dlname)
        out_fd = AtomicFile(dlname, 'w')
        for url in self.inputs:
            fd, sze, url = self.task.connect_input(url)
            for k, v in self.task.reader(fd, sze, url):
                if ' ' in k:
                    TaskFailed("Spaces are not allowed in keys "\
                               "with external sort.")
                if '\0' in v:
                    TaskFailed("Zero bytes are not allowed in "\
                               "values with external sort. "\
                               "Consider using base64 encoding.")
                out_fd.write('%s %s\0' % (k, v))
        out_fd.close()
        Message("Reduce input downloaded ok")

        Message("Starting external sort")
        sortname = self.task.path('REDUCE_SORTED', self.task.id)
        ensure_path(os.path.dirname(sortname))
        cmd = ['sort', '-n', '-k', '1,1', '-z', '-t', ' ', '-o', sortname, dlname]

        proc = subprocess.Popen(cmd)
        ret = proc.wait()
        if ret:
            TaskFailed("Sorting %s to %s failed (%d)" % (dlname, sortname, ret))

        Message("External sort done: %s" % sortname)
        def reader(fd, sze, url):
            return func.re_reader('(?s)(.*?) (.*?)\000', fd, sze, url)
        return self.multi_file_iterator(reader, inputs=[sortname])

    def memory_sort(self):
        Message("Sorting in memory")
        m = list(self.multi_file_iterator(self.task.reader, progress=False))
        return self.task.track_status(sorted(m, cmp=num_cmp), "%s entries reduced")

    def multi_file_iterator(self, reader, progress=True, inputs=None):
        def entries():
            for url in (inputs or self.inputs):
                fd, sze, url = self.task.connect_input(url)
                for entry in reader(fd, sze, url):
                    yield entry
        if progress:
            return self.task.track_status(entries(), "%s entries reduced")
        return entries()
