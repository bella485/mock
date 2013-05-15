# vim:expandtab:autoindent:tabstop=4:shiftwidth=4:filetype=python:textwidth=0:
# License: GPL2 or later see COPYING
# Written by Michael Brown
# Sections by Seth Vidal
# Sections taken from Mach by Thomas Vander Stichele
# Copyright (C) 2007 Michael E Brown <mebrown@michaels-house.net>

# python library imports
import ctypes
import fcntl
import os
import os.path
import rpm
import rpmUtils
import rpmUtils.transaction
import select
import shutil
import subprocess
import time
import errno
import grp
from glob import glob

# our imports
import mockbuild.exception
from mockbuild.trace_decorator import traceLog, decorate, getLog
import mockbuild.uid as uid

_libc = ctypes.cdll.LoadLibrary(None)
_errno = ctypes.c_int.in_dll(_libc, "errno")
_libc.personality.argtypes = [ctypes.c_ulong]
_libc.personality.restype = ctypes.c_int
_libc.unshare.argtypes = [ctypes.c_int,]
_libc.unshare.restype = ctypes.c_int
# See linux/include/sched.h
CLONE_NEWNS = 0x00020000
CLONE_NEWUTS = 0x04000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000
CLONE_NEWIPC = 0x08000000

# taken from sys/personality.h
PER_LINUX32=0x0008
PER_LINUX=0x0000
personality_defs = {
    'x86_64': PER_LINUX, 'ppc64': PER_LINUX, 'sparc64': PER_LINUX,
    'i386': PER_LINUX32, 'i586': PER_LINUX32, 'i686': PER_LINUX32,
    'ppc': PER_LINUX32, 'sparc': PER_LINUX32, 'sparcv9': PER_LINUX32,
    'ia64' : PER_LINUX, 'alpha' : PER_LINUX,
    's390' : PER_LINUX32, 's390x' : PER_LINUX,
}

# classes
class commandTimeoutExpired(mockbuild.exception.Error):
    def __init__(self, msg):
        mockbuild.exception.Error.__init__(self, msg)
        self.msg = msg
        self.resultcode = 10

# functions
decorate(traceLog())
def mkdirIfAbsent(*args):
    for dirName in args:
        getLog().debug("ensuring that dir exists: %s" % dirName)
        if not os.path.exists(dirName):
            try:
                getLog().debug("creating dir: %s" % dirName)
                os.makedirs(dirName)
            except OSError, e:
                getLog().exception("Could not create dir %s. Error: %s" % (dirName, e))
                raise mockbuild.exception.Error, "Could not create dir %s. Error: %s" % (dirName, e)

decorate(traceLog())
def touch(fileName):
    getLog().debug("touching file: %s" % fileName)
    fo = open(fileName, 'w')
    fo.close()

decorate(traceLog())
def rmtree(path, *args, **kargs):
    """version os shutil.rmtree that ignores no-such-file-or-directory errors,
       and tries harder if it finds immutable files"""
    do_selinux_ops = False
    if kargs.has_key('selinux'):
        do_selinux_ops = kargs['selinux']
        del kargs['selinux']
    tryAgain = 1
    retries = 0
    failedFilename = None
    getLog().debug("remove tree: %s" % path)
    while tryAgain:
        tryAgain = 0
        try:
            shutil.rmtree(path, *args, **kargs)
        except OSError, e:
            if e.errno == errno.ENOENT: # no such file or directory
                pass
            elif do_selinux_ops and (e.errno==errno.EPERM or e.errno==errno.EACCES):
                tryAgain = 1
                if failedFilename == e.filename:
                    raise
                failedFilename = e.filename
                os.system("chattr -R -i %s" % path)
            elif e.errno == errno.EBUSY:
                retries += 1
                if retries > 1:
                    raise
                tryAgain = 1
                getLog().debug("retrying failed tree remove after sleeping a bit")
                time.sleep(2)
            else:
                raise

from signal import SIGTERM
decorate(traceLog())
def orphansKill(rootToKill, killsig=SIGTERM):
    """kill off anything that is still chrooted."""
    getLog().debug("kill orphans")
    for fn in [ d for d in os.listdir("/proc") if d.isdigit() ]:
        try:
            root = os.readlink("/proc/%s/root" % fn)
            if os.path.realpath(root) == os.path.realpath(rootToKill):
                getLog().warning("Process ID %s still running in chroot. Killing..." % fn)
                pid = int(fn, 10)
                os.kill(pid, killsig)
                os.waitpid(pid, 0)
        except OSError, e:
            pass


decorate(traceLog())
def yieldSrpmHeaders(srpms, plainRpmOk=0):
    ts = rpmUtils.transaction.initReadOnlyTransaction()
    for srpm in srpms:
        try:
            hdr = rpmUtils.miscutils.hdrFromPackage(ts, srpm)
        except (rpmUtils.RpmUtilsError,), e:
            raise mockbuild.exception.Error, "Cannot find/open srpm: %s. Error: %s" % (srpm, ''.join(e))

        if not plainRpmOk and hdr[rpm.RPMTAG_SOURCEPACKAGE] != 1:
            raise mockbuild.exception.Error("File is not an srpm: %s." % srpm )

        yield hdr

decorate(traceLog())
def getNEVRA(hdr):
    name = hdr[rpm.RPMTAG_NAME]
    ver  = hdr[rpm.RPMTAG_VERSION]
    rel  = hdr[rpm.RPMTAG_RELEASE]
    epoch = hdr[rpm.RPMTAG_EPOCH]
    arch = hdr[rpm.RPMTAG_ARCH]
    if epoch is None: epoch = 0
    return (name, epoch, ver, rel, arch)

decorate(traceLog())
def cmpKernelVer(str1, str2):
    'compare two kernel version strings and return -1, 0, 1 for less, equal, greater'
    # first try compareVerOnly and fall back to compareEVR
    try:
        return rpmUtils.miscutils.compareVerOnly(str1, str2)
    except AttributeError, e:
        return rpmUtils.miscutils.compareEVR(('', str1, ''), ('', str2, ''))

decorate(traceLog())
def getAddtlReqs(hdr, conf):
    # Add the 'more_buildreqs' for this SRPM (if defined in config file)
    (name, epoch, ver, rel, arch) = getNEVRA(hdr)
    reqlist = []
    for this_srpm in ['-'.join([name, ver, rel]),
                      '-'.join([name, ver]),
                      '-'.join([name]),]:
        if conf.has_key(this_srpm):
            more_reqs = conf[this_srpm]
            if type(more_reqs) in (type(u''), type(''),):
                reqlist.append(more_reqs)
            else:
                reqlist.extend(more_reqs)
            break

    return rpmUtils.miscutils.unique(reqlist)

# not traced...
def chomp(line):
    if line.endswith("\n"):
        return line[:-1]
    else:
        return line

decorate(traceLog())
def unshare(flags):
    getLog().debug("Unsharing. Flags: %s" % flags)
    try:
        res = _libc.unshare(flags)
        if res:
            raise mockbuild.exception.UnshareFailed(os.strerror(_errno.value))
    except AttributeError, e:
        pass

# these are called in child process, so no logging
def condChroot(chrootPath):
    if chrootPath is not None:
        saved = { "ruid": os.getuid(), "euid": os.geteuid(), }
        uid.setresuid(0,0,0)
        os.chdir(chrootPath)
        os.chroot(chrootPath)
        uid.setresuid(saved['ruid'], saved['euid'])

def condChdir(cwd):
    if cwd is not None:
        os.chdir(cwd)

def condDropPrivs(uid, gid):
    if gid is not None:
        os.setregid(gid, gid)
    if uid is not None:
        os.setreuid(uid, uid)

def condPersonality(per=None):
    if per is None or per in ('noarch',):
        return
    if personality_defs.get(per, None) is None:
        return
    res = _libc.personality(personality_defs[per])
    if res == -1:
        raise OSError(_errno.value, os.strerror(_errno.value))

def condEnvironment(env=None):
    if not env:
        return
    getLog().debug("child environment: %s" % env)
    os.environ.clear()
    for k in env.keys():
        os.putenv(k, env[k])

def logOutput(fds, logger, returnOutput=1, start=0, timeout=0, printOutput=False):
    output=""
    done = 0

    # set all fds to nonblocking
    for fd in fds:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if not fd.closed:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags| os.O_NONBLOCK)

    tail = ""
    while not done:
        if (time.time() - start)>timeout and timeout!=0:
            done = 1
            break

        i_rdy,o_rdy,e_rdy = select.select(fds,[],[],1)
        for s in i_rdy:
            # slurp as much input as is ready
            input = s.read()
            if input == "":
                done = 1
                break
            if logger is not None:
                lines = input.split("\n")
                if tail:
                    lines[0] = tail + lines[0]
                # we may not have all of the last line
                tail = lines.pop()
                for line in lines:
                    if line == '': continue
                    logger.debug(line)
                for h in logger.handlers:
                    h.flush()
            if returnOutput:
                output += input
            if printOutput:
                print input,

    if tail and logger is not None:
        logger.debug(tail)
    return output

decorate(traceLog())
def selinuxEnabled():
    """Check if SELinux is enabled (enforcing or permissive)."""
    for mount in open("/proc/mounts").readlines():
        (fstype, mountpoint, garbage) = mount.split(None, 2)
        if fstype == "selinuxfs":
            selinux_mountpoint = mountpoint
            break
    else:
        selinux_mountpoint = "/selinux"

    try:
        enforce_filename = os.path.join(selinux_mountpoint, "enforce")
        if open(enforce_filename).read().strip() in ("1", "0"):
            return True
    except:
        pass
    return False

# logger =
# output = [1|0]
# chrootPath
#
# The "Not-as-complicated" version
#
decorate(traceLog())
def do(command, shell=False, chrootPath=None, cwd=None, timeout=0, raiseExc=True,
       returnOutput=0, uid=None, gid=None, personality=None,
       printOutput=False, env=None, *args, **kargs):

    logger = kargs.get("logger", getLog())
    output = ""
    start = time.time()
    preexec = ChildPreExec(personality, chrootPath, cwd, uid, gid)
    if env is None:
        env = clean_env()
    try:
        child = None
        logger.debug("Executing command: %s with env %s" % (command, env))
        child = subprocess.Popen(
            command,
            shell=shell,
            env=env,
            bufsize=0, close_fds=True,
            stdin=open("/dev/null", "r"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn = preexec,
            )

        # use select() to poll for output so we dont block
        output = logOutput([child.stdout, child.stderr],
                           logger, returnOutput, start, timeout, printOutput=printOutput)

    except:
        # kill children if they arent done
        if child is not None and child.returncode is None:
            os.killpg(child.pid, 9)
        try:
            if child is not None:
                os.waitpid(child.pid, 0)
        except:
            pass
        raise

    # wait until child is done, kill it if it passes timeout
    niceExit=1
    while child.poll() is None:
        if (time.time() - start)>timeout and timeout!=0:
            niceExit=0
            os.killpg(child.pid, 15)
        if (time.time() - start)>(timeout+1) and timeout!=0:
            niceExit=0
            os.killpg(child.pid, 9)

    if not niceExit:
        raise commandTimeoutExpired, ("Timeout(%s) expired for command:\n # %s\n%s" % (timeout, command, output))

    logger.debug("Child return code was: %s" % str(child.returncode))
    if raiseExc and child.returncode:
        if returnOutput:
            raise mockbuild.exception.Error, ("Command failed: \n # %s\n%s" % (command, output), child.returncode)
        else:
            raise mockbuild.exception.Error, ("Command failed. See logs for output.\n # %s" % (command,), child.returncode)

    return output

class ChildPreExec(object):
    def __init__(self, personality, chrootPath, cwd, uid, gid, env=None, shell=False):
        self.personality = personality
        self.chrootPath  = chrootPath
        self.cwd = cwd
        self.uid = uid
        self.gid = gid
        self.env = env
        self.shell = shell

    def __call__(self, *args, **kargs):
        if not self.shell:
            os.setsid()
        os.umask(002)
        condPersonality(self.personality)
        condEnvironment(self.env)
        condChroot(self.chrootPath)
        condDropPrivs(self.uid, self.gid)
        condChdir(self.cwd)

def is_in_dir(path, directory):
    """Tests whether `path` is inside `directory`."""
    # use realpath to expand symlinks
    path = os.path.realpath(path)
    directory = os.path.realpath(directory)

    return os.path.commonprefix([path, directory]) == directory


def doshell(chrootPath=None, environ=None, uid=None, gid=None, cmd=None):
    log = getLog()
    log.debug("doshell: chrootPath:%s, uid:%d, gid:%d" % (chrootPath, uid, gid))
    if environ is None:
        environ = clean_env()
    if not 'PROMPT_COMMAND' in environ:
        environ['PROMPT_COMMAND'] = 'echo -n "<mock-chroot>"'
    if not 'SHELL' in environ:
        environ['SHELL'] = '/bin/bash'
    log.debug("doshell environment: %s", environ)
    if cmd:
        cmdstr = '/bin/bash -c "%s"' % cmd
    else:
        cmdstr = "/bin/bash -i -l"
    preexec = ChildPreExec(personality=None, chrootPath=chrootPath, cwd=None,
                           uid=uid, gid=gid, env=environ, shell=True)
    log.debug("doshell: command: %s" % cmdstr)
    return subprocess.call(cmdstr, preexec_fn=preexec, env=environ, shell=True)



def run(cmd, isShell=True):
    log = getLog()
    log.debug("run: cmd = %s\n" % cmd)
    return subprocess.call(cmd, shell=isShell)

def clean_env():
    env = {'TERM' : 'vt100',
           'SHELL' : '/bin/bash',
           'HOME' : '/builddir',
           'HOSTNAME' : 'mock',
           'PATH' : '/usr/bin:/bin:/usr/sbin:/sbin',
           }
    env['LANG'] = os.environ.setdefault('LANG', 'en_US.UTF-8')
    return env

def get_fs_type(path):
    cmd = '/usr/bin/stat -f -L -c %%T %s' % path
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
    p.wait()
    return p.stdout.readline().strip()

def find_non_nfs_dir():
    dirs = ('/tmp', '/usr/tmp', '/')
    for d in dirs:
        if not get_fs_type(d).startswith('nfs'):
            return d
    raise mockbuild.exception.Error('Cannot find non-NFS directory in: %s' % dirs)


decorate(traceLog())
def setup_default_config_opts(unprivUid, version, pkgpythondir):
    "sets up default configuration."
    config_opts = {}
    config_opts['version'] = version
    config_opts['basedir'] = '/var/lib/mock' # root name is automatically added to this
    config_opts['resultdir'] = '%(basedir)s/%(root)s/result'
    config_opts['cache_topdir'] = '/var/cache/mock'
    config_opts['clean'] = True
    config_opts['chroothome'] = '/builddir'
    config_opts['log_config_file'] = 'logging.ini'
    config_opts['rpmbuild_timeout'] = 0
    config_opts['chrootuid'] = unprivUid
    try:
        config_opts['chrootgid'] = grp.getgrnam("mock")[2]
    except KeyError:
        #  'mock' group doesn't exist, must set in config file
        pass
    config_opts['build_log_fmt_name'] = "unadorned"
    config_opts['root_log_fmt_name']  = "detailed"
    config_opts['state_log_fmt_name'] = "state"
    config_opts['online'] = True

    config_opts['internal_dev_setup'] = True
    config_opts['internal_setarch'] = True

    # cleanup_on_* only take effect for separate --resultdir
    # config_opts provides fine-grained control. cmdline only has big hammer
    config_opts['cleanup_on_success'] = True
    config_opts['cleanup_on_failure'] = True

    config_opts['createrepo_on_rpms'] = False
    config_opts['createrepo_command'] = '/usr/bin/createrepo -d -q -x *.src.rpm' # default command

    config_opts['backup_on_clean'] = False
    config_opts['backup_base_dir'] = os.path.join(config_opts['basedir'], "backup")

    # (global) plugins and plugin configs.
    # ordering constraings: tmpfs must be first.
    #    root_cache next.
    #    after that, any plugins that must create dirs (yum_cache)
    #    any plugins without preinit hooks should be last.
    config_opts['plugins'] = ['tmpfs', 'root_cache', 'yum_cache', 'bind_mount', 'ccache', 'selinux',
                              'package_state', 'chroot_scan']
    config_opts['plugin_dir'] = os.path.join(pkgpythondir, "plugins")
    config_opts['plugin_conf'] = {
            'ccache_enable': True,
            'ccache_opts': {
                'max_cache_size': "4G",
                'compress': None,
                'dir': "%(cache_topdir)s/%(root)s/ccache/"},
            'yum_cache_enable': True,
            'yum_cache_opts': {
                'max_age_days': 30,
                'max_metadata_age_days': 30,
                'dir': "%(cache_topdir)s/%(root)s/yum_cache/",
                'online': True,},
            'root_cache_enable': True,
            'root_cache_opts': {
                'age_check' : True,
                'max_age_days': 15,
                'dir': "%(cache_topdir)s/%(root)s/root_cache/",
                'compress_program': 'pigz',
                'exclude_dirs': ["./proc", "./sys", "./dev", "./tmp/ccache", "./var/cache/yum" ],
                'extension': '.gz'},
            'bind_mount_enable': True,
            'bind_mount_opts': {
            	'dirs': [
                # specify like this:
                # ('/host/path', '/bind/mount/path/in/chroot/' ),
                # ('/another/host/path', '/another/bind/mount/path/in/chroot/'),
                ],
                'create_dirs': False,},
            'mount_enable': True,
            'mount_opts': {'dirs': [
                # specify like this:
                # ("/dev/device", "/mount/path/in/chroot/", "vfstype", "mount_options"),
                ]},
            'tmpfs_enable': False,
            'tmpfs_opts': {
                'required_ram_mb': 900,
                'max_fs_size': None},
            'selinux_enable': True,
            'selinux_opts': {},
            'package_state_enable' : True,
            'package_state_opts' : {},
            'chroot_scan_enable': False,
            'chroot_scan_opts': { 'regexes' : [
                "\\bcore(\\.\\d+)?$",
                "\\.log$",
                ]},
            }

    config_opts['environment'] = {
        'TERM': 'vt100',
        'SHELL': '/bin/bash',
        'HOME': '/builddir',
        'HOSTNAME': 'mock',
        'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
        'PROMPT_COMMAND': 'echo -n "<mock-chroot>"',
        'LANG': os.environ.setdefault('LANG', 'en_US.UTF-8'),
        }

    runtime_plugins = [runtime_plugin
                       for (runtime_plugin, _)
                       in [os.path.splitext(os.path.basename(tmp_path))
                           for tmp_path
                           in glob(config_opts['plugin_dir'] + "/*.py")]
                       if runtime_plugin not in config_opts['plugins']]
    for runtime_plugin in sorted(runtime_plugins):
        config_opts['plugins'].append(runtime_plugin)
        config_opts['plugin_conf'][runtime_plugin + "_enable"] = False
        config_opts['plugin_conf'][runtime_plugin + "_opts"] = {}

    # SCM defaults
    config_opts['scm'] = False
    config_opts['scm_opts'] = {
            'method': 'git',
            'cvs_get': 'cvs -d /srv/cvs co SCM_BRN SCM_PKG',
            'git_get': 'git clone SCM_BRN git://localhost/SCM_PKG.git SCM_PKG',
            'svn_get': 'svn co file:///srv/svn/SCM_PKG/SCM_BRN SCM_PKG',
            'spec': 'SCM_PKG.spec',
            'ext_src_dir': '/dev/null',
            'write_tar': False,
            'git_timestamps': False,
            }

    # dependent on guest OS
    config_opts['useradd'] = \
        '/usr/sbin/useradd -o -m -u %(uid)s -g %(gid)s -d %(home)s -n %(user)s'
    config_opts['use_host_resolv'] = True
    config_opts['chroot_setup_cmd'] = ('groupinstall', 'buildsys-build')
    config_opts['target_arch'] = 'i386'
    config_opts['rpmbuild_arch'] = None # <-- None means set automatically from target_arch
    config_opts['yum.conf'] = ''
    config_opts['yum_builddep_opts'] = ''
    config_opts['yum_common_opts'] = []
    config_opts['priorities.conf'] = '\n[main]\nenabled=0'
    config_opts['rhnplugin.conf'] = '\n[main]\nenabled=0'
    config_opts['more_buildreqs'] = {}
    config_opts['files'] = {}
    config_opts['macros'] = {
        '%_topdir': '%s/build' % config_opts['chroothome'],
        '%_rpmfilename': '%%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm',
        }
    # security config
    config_opts['no_root_shells'] = False

    return config_opts

decorate(traceLog())
def set_config_opts_per_cmdline(config_opts, options, args):
    "takes processed cmdline args and sets config options."
    # do some other options and stuff
    if options.arch:
        config_opts['target_arch'] = options.arch
    if options.rpmbuild_arch:
        config_opts['rpmbuild_arch'] = options.rpmbuild_arch
    elif config_opts['rpmbuild_arch'] is None:
        config_opts['rpmbuild_arch'] = config_opts['target_arch']

    if not options.clean:
        config_opts['clean'] = options.clean

    for option in options.rpmwith:
        options.rpmmacros.append("_with_%s --with-%s" %
                                 (option.replace("-", "_"), option))

    for option in options.rpmwithout:
        options.rpmmacros.append("_without_%s --without-%s" %
                                 (option.replace("-", "_"), option))

    for macro in options.rpmmacros:
        try:
            k, v = macro.split(" ", 1)
            if not k.startswith('%'):
                k = '%%%s' % k
            config_opts['macros'].update({k: v})
        except:
            raise mockbuild.exception.BadCmdline(
                "Bad option for '--define' (%s).  Use --define 'macro expr'"
                % macro)

    if options.resultdir:
        config_opts['resultdir'] = os.path.expanduser(options.resultdir)
    if options.uniqueext:
        config_opts['unique-ext'] = options.uniqueext
    if options.rpmbuild_timeout is not None:
        config_opts['rpmbuild_timeout'] = options.rpmbuild_timeout

    for i in options.disabled_plugins:
        if i not in config_opts['plugins']:
            raise mockbuild.exception.BadCmdline(
                "Bad option for '--disable-plugin=%s'. Expecting one of: %s"
                % (i, config_opts['plugins']))
        config_opts['plugin_conf']['%s_enable' % i] = False
    for i in options.enabled_plugins:
        if i not in config_opts['plugins']:
            raise mockbuild.exception.BadCmdline(
                "Bad option for '--enable-plugin=%s'. Expecting one of: %s"
                % (i, config_opts['plugins']))
        config_opts['plugin_conf']['%s_enable' % i] = True
    for option in options.plugin_opts:
        try:
            p, kv = option.split(":", 1)
            k, v  = kv.split("=", 1)
        except:
            raise mockbuild.exception.BadCmdline(
                "Bad option for '--plugin-option' (%s).  Use --plugin-option 'plugin:key=value'"
                % option)
        if p not in config_opts['plugins']:
            raise mockbuild.exception.BadCmdline(
                "Bad option for '--plugin-option' (%s).  No such plugin: %s"
                % (option, p))
        if v == "False": v = False
        if v == "True": v = True
        config_opts['plugin_conf'][p + "_opts"].update({k: v})


    if options.mode in ("rebuild",) and len(args) > 1 and not options.resultdir:
        raise mockbuild.exception.BadCmdline(
            "Must specify --resultdir when building multiple RPMS.")

    if options.cleanup_after == False:
        config_opts['cleanup_on_success'] = False
        config_opts['cleanup_on_failure'] = False

    if options.cleanup_after == True:
        config_opts['cleanup_on_success'] = True
        config_opts['cleanup_on_failure'] = True
    # can't cleanup unless resultdir is separate from the root dir
    rootdir = os.path.join(config_opts['basedir'], config_opts['root'])
    if mockbuild.util.is_in_dir(config_opts['resultdir'] % config_opts, rootdir):
        config_opts['cleanup_on_success'] = False
        config_opts['cleanup_on_failure'] = False

    config_opts['cache_alterations'] = options.cache_alterations

    config_opts['online'] = options.online

    if options.scm:
        try:
            from mockbuild import scm
        except Exception as e:
            raise mockbuild.exception.BadCmdline(
                "Mock SCM module not installed: %s" % e)

        config_opts['scm'] = options.scm
        for option in options.scm_opts:
            try:
                k, v = option.split("=", 1)
                config_opts['scm_opts'].update({k: v})
            except:
                raise mockbuild.exception.BadCmdline(
                "Bad option for '--scm-option' (%s).  Use --scm-option 'key=value'"
                % option)
