
__author__    = "Andre Merzky, Ole Weidner"
__copyright__ = "Copyright 2012-2013, The SAGA Project"
__license__   = "MIT"


""" PBS job adaptor implementation
"""

import threading

import saga.url             as surl
import saga.utils.pty_shell as sups
import saga.adaptors.base
import saga.adaptors.cpi.job

from saga.job.constants import *

import re
import os 
import time
import threading

from cgi  import parse_qs

SYNC_CALL  = saga.adaptors.cpi.decorators.SYNC_CALL
ASYNC_CALL = saga.adaptors.cpi.decorators.ASYNC_CALL

SYNC_WAIT_UPDATE_INTERVAL =  1  # seconds
MONITOR_UPDATE_INTERVAL   = 60  # seconds


# --------------------------------------------------------------------
#
class _job_state_monitor(threading.Thread):
    """ thread that periodically monitors job states
    """
    def __init__(self, job_service):

        self.logger = job_service._logger
        self.js = job_service
        self._stop = threading.Event()

        super(_job_state_monitor, self).__init__()
        self.setDaemon(True)

    def stop(self):
        self._stop.set()


    def stopped(self):
        return self._stop.isSet()

    def run(self):

        while self.stopped() is False:

            try:
                # FIXME: do bulk updates here! we don't want to pull information
                # job by job. that would be too inefficient!
                jobs = self.js.jobs

                for job_id in jobs.keys() :

                    job_info = jobs[job_id]

                    # we only need to monitor jobs that are not in a
                    # terminal state, so we can skip the ones that are 
                    # either done, failed or canceled
                    if  job_info['state'] not in [saga.job.DONE, saga.job.FAILED, saga.job.CANCELED] :

                        new_job_info = self.js._job_get_info(job_id)
                        self.logger.info ("Job monitoring thread updating Job %s (state: %s)" \
                                       % (job_id, new_job_info['state']))

                        # fire job state callback if 'state' has changed
                        if  new_job_info['state'] != job_info['state']:
                            job_obj = job_info['obj']
                            job_obj._attributes_i_set('state', new_job_info['state'], job_obj._UP, True)

                        # update job info
                        jobs[job_id] = new_job_info

            except Exception as e:
                import traceback
                traceback.print_exc ()
                self.logger.warning("Exception caught in job monitoring thread: %s" % e)

            finally :
                time.sleep (MONITOR_UPDATE_INTERVAL)


# --------------------------------------------------------------------
#
def log_error_and_raise(message, exception, logger):
    """ loggs an 'error' message and subsequently throws an exception
    """
    logger.error(message)
    raise exception(message)


# --------------------------------------------------------------------
#
def _pbs_to_saga_jobstate(pbsjs):
    """ translates a pbs one-letter state to saga
    """
    if pbsjs == 'C': # Torque
        return saga.job.DONE
    elif pbsjs == 'F': # PBS Pro
        return saga.job.DONE
    elif pbsjs == 'E':
        return saga.job.RUNNING
    elif pbsjs == 'H':
        return saga.job.PENDING
    elif pbsjs == 'Q':
        return saga.job.PENDING
    elif pbsjs == 'R':
        return saga.job.RUNNING
    elif pbsjs == 'T':
        return saga.job.RUNNING
    elif pbsjs == 'W':
        return saga.job.PENDING
    elif pbsjs == 'S':
        return saga.job.PENDING
    elif pbsjs == 'X':
        return saga.job.CANCELED
    else:
        return saga.job.UNKNOWN


# --------------------------------------------------------------------
#
def _pbscript_generator(url, logger, jd, ppn, pbs_version, is_cray=False, queue=None, ):
    """ generates a PBS script from a SAGA job description
    """
    pbs_params = str()
    exec_n_args = str()

    if jd.executable is not None:
        exec_n_args += "%s " % (jd.executable)
    if jd.arguments is not None:
        for arg in jd.arguments:
            exec_n_args += "%s " % (arg)

    if jd.name is not None:
        pbs_params += "#PBS -N %s \n" % jd.name

    if (is_cray is "") or not('Version: 4.2.7' in pbs_version):
        # qsub on Cray systems complains about the -V option:
        # Warning:
        # Your job uses the -V option, which requests that all of your
        # current shell environment settings (9913 bytes) be exported to
        # it.  This is not recommended, as it causes problems for the
        # batch environment in some cases.
        pbs_params += "#PBS -V \n"

    if jd.environment is not None:
        variable_list = str()
        for key in jd.environment.keys():
            variable_list += "%s=%s," % (key, jd.environment[key])
        pbs_params += "#PBS -v %s \n" % variable_list

# apparently this doesn't work with older PBS installations
#    if jd.working_directory is not None:
#        pbs_params += "#PBS -d %s \n" % jd.working_directory

    # a workaround is to do an explicit 'cd'
    if jd.working_directory is not None:
        workdir_directives  = 'export    PBS_O_WORKDIR=%s \n' % jd.working_directory
        workdir_directives += 'mkdir -p  %s\n' % jd.working_directory
        workdir_directives += 'cd        %s\n' % jd.working_directory
    else:
        workdir_directives = ''

    if jd.output is not None:
        # if working directory is set, we want stdout to end up in
        # the working directory as well, unless it containes a specific
        # path name.
        if jd.working_directory is not None:
            if os.path.isabs(jd.output):
                pbs_params += "#PBS -o %s \n" % jd.output
            else:
                # user provided a relative path for STDOUT. in this case 
                # we prepend the workind directory path before passing
                # it on to PBS
                pbs_params += "#PBS -o %s/%s \n" % (jd.working_directory, jd.output)
        else:
            pbs_params += "#PBS -o %s \n" % jd.output

    if jd.error is not None:
        # if working directory is set, we want stderr to end up in 
        # the working directory as well, unless it contains a specific
        # path name. 
        if jd.working_directory is not None:
            if os.path.isabs(jd.error):
                pbs_params += "#PBS -e %s \n" % jd.error
            else:
                # user provided a realtive path for STDERR. in this case 
                # we prepend the workind directory path before passing
                # it on to PBS
                pbs_params += "#PBS -e %s/%s \n" % (jd.working_directory, jd.error)
        else:
            pbs_params += "#PBS -e %s \n" % jd.error


    if jd.wall_time_limit is not None:
        hours = jd.wall_time_limit / 60
        minutes = jd.wall_time_limit % 60
        pbs_params += "#PBS -l walltime=%s:%s:00 \n" \
            % (str(hours), str(minutes))

    if (jd.queue is not None) and (queue is not None):
        pbs_params += "#PBS -q %s \n" % queue
    elif (jd.queue is not None) and (queue is None):
        pbs_params += "#PBS -q %s \n" % jd.queue
    elif (jd.queue is None) and (queue is not None):
        pbs_params += "#PBS -q %s \n" % queue

    if jd.project is not None:
        if 'PBSPro_1' in pbs_version:
            # On PBS Pro we set both -P(roject) and -A(accounting),
            # as we don't know what the admins decided, and just
            # pray that this doesn't create problems.
            pbs_params += "#PBS -P %s \n" % str(jd.project)
            pbs_params += "#PBS -A %s \n" % str(jd.project)
        else:
            # Torque
            pbs_params += "#PBS -A %s \n" % str(jd.project)

    if jd.job_contact is not None:
        pbs_params += "#PBS -m abe \n"

    # if total_cpu_count is not defined, we assume 1
    if jd.total_cpu_count is None:
        jd.total_cpu_count = 1

    tcc = jd.total_cpu_count
    nnodes = tcc / ppn
    if tcc % ppn > 0:
        nnodes += 1 # Request enough nodes to cater for the number of cores requested

    if is_cray is not "":
        # Special cases for PBS/TORQUE on Cray. Different PBSes,
        # different flags. A complete nightmare...
        if 'PBSPro_10' in pbs_version:
            logger.info("Using Cray XT (e.g. Hopper) specific '#PBS -l mppwidth=xx' flags (PBSPro_10).")
            pbs_params += "#PBS -l mppwidth=%s \n" % jd.total_cpu_count
        elif 'PBSPro_12' in pbs_version:
            logger.info("Using Cray XT (e.g. Archer) specific '#PBS -l select=xx' flags (PBSPro_12).")
            pbs_params += "#PBS -l select=%d\n" % nnodes
        elif '4.2.6' in pbs_version:
            logger.info("Using Titan (Cray XP) specific '#PBS -l nodes=xx'")
            pbs_params += "#PBS -l nodes=%d\n" % nnodes
        elif '4.2.7' in pbs_version:
            logger.info("Using Cray XT @ NERSC (e.g. Edison) specific '#PBS -l mppwidth=xx' flags (PBSPro_10).")
            pbs_params += "#PBS -l mppwidth=%s \n" % jd.total_cpu_count
        else:
            logger.info("Using Cray XT (e.g. Kraken, Jaguar) specific '#PBS -l size=xx' flags (TORQUE).")
            pbs_params += "#PBS -l size=%s\n" % jd.total_cpu_count
    elif 'version: 2.3.13' in pbs_version:
        # e.g. Blacklight
        # TODO: The more we add, the more it screams for a refactoring
        pbs_params += "#PBS -l ncpus=%d\n" % tcc
    elif '4.2.7' in pbs_version:
        logger.info("Using Cray XT @ NERSC (e.g. Hopper) specific '#PBS -l mppwidth=xx' flags (PBSPro_10).")
        pbs_params += "#PBS -l mppwidth=%s \n" % jd.total_cpu_count
    elif  'PBSPro_12' in pbs_version:
        logger.info("Using PBSPro 12 notation '#PBS -l select=XX' ")
        pbs_params += "#PBS -l select=%d\n" % (nnodes)
    else:
        # Default case, i.e, standard HPC cluster (non-Cray)

        # If we want just a slice of one node
        if jd.total_cpu_count < ppn:
            ppn = jd.total_cpu_count

        pbs_params += "#PBS -l nodes=%d:ppn=%d \n" % (nnodes, ppn)

    # escape all double quotes and dollarsigns, otherwise 'echo |'
    # further down won't work
    # only escape '$' in args and exe. not in the params
    exec_n_args = workdir_directives + exec_n_args
    exec_n_args = exec_n_args.replace('$', '\\$')

    pbscript = "\n#!/bin/bash \n%s%s" % (pbs_params, exec_n_args)

    pbscript = pbscript.replace('"', '\\"')
    return pbscript


# --------------------------------------------------------------------
# some private defs
#
_PTY_TIMEOUT = 2.0

# --------------------------------------------------------------------
# the adaptor name
#
_ADAPTOR_NAME          = "saga.adaptor.pbsjob"
_ADAPTOR_SCHEMAS       = ["pbs", "pbs+ssh", "pbs+gsissh"]
_ADAPTOR_OPTIONS       = []

# --------------------------------------------------------------------
# the adaptor capabilities & supported attributes
#
_ADAPTOR_CAPABILITIES = {
    "jdes_attributes":   [saga.job.NAME,
                          saga.job.EXECUTABLE,
                          saga.job.ARGUMENTS,
                          saga.job.ENVIRONMENT,
                          saga.job.INPUT,
                          saga.job.OUTPUT,
                          saga.job.ERROR,
                          saga.job.QUEUE,
                          saga.job.PROJECT,
                          saga.job.WALL_TIME_LIMIT,
                          saga.job.WORKING_DIRECTORY,
                          saga.job.WALL_TIME_LIMIT,
                          saga.job.SPMD_VARIATION, # TODO: 'hot'-fix for BigJob
                          saga.job.TOTAL_CPU_COUNT],
    "job_attributes":    [saga.job.EXIT_CODE,
                          saga.job.EXECUTION_HOSTS,
                          saga.job.CREATED,
                          saga.job.STARTED,
                          saga.job.FINISHED],
    "metrics":           [saga.job.STATE],
    "callbacks":         [saga.job.STATE],
    "contexts":          {"ssh": "SSH public/private keypair",
                          "x509": "GSISSH X509 proxy context",
                          "userpass": "username/password pair (ssh)"}
}

# --------------------------------------------------------------------
# the adaptor documentation
#
_ADAPTOR_DOC = {
    "name":          _ADAPTOR_NAME,
    "cfg_options":   _ADAPTOR_OPTIONS,
    "capabilities":  _ADAPTOR_CAPABILITIES,
    "description":  """
The PBS adaptor allows to run and manage jobs on `PBS <http://www.pbsworks.com/>`_
and `TORQUE <http://www.adaptivecomputing.com/products/open-source/torque>`_
controlled HPC clusters.
""",
    "example": "examples/jobs/pbsjob.py",
    "schemas": {"pbs":        "connect to a local cluster",
                "pbs+ssh":    "conenct to a remote cluster via SSH",
                "pbs+gsissh": "connect to a remote cluster via GSISSH"}
}

# --------------------------------------------------------------------
# the adaptor info is used to register the adaptor with SAGA
#
_ADAPTOR_INFO = {
    "name"        :    _ADAPTOR_NAME,
    "version"     : "v0.1",
    "schemas"     : _ADAPTOR_SCHEMAS,
    "capabilities":  _ADAPTOR_CAPABILITIES,
    "cpis": [
        {
        "type": "saga.job.Service",
        "class": "PBSJobService"
        },
        {
        "type": "saga.job.Job",
        "class": "PBSJob"
        }
    ]
}


###############################################################################
# The adaptor class
class Adaptor (saga.adaptors.base.Base):
    """ this is the actual adaptor class, which gets loaded by SAGA (i.e. by 
        the SAGA engine), and which registers the CPI implementation classes 
        which provide the adaptor's functionality.
    """

    # ----------------------------------------------------------------
    #
    def __init__(self):

        saga.adaptors.base.Base.__init__(self, _ADAPTOR_INFO, _ADAPTOR_OPTIONS)

        self.id_re = re.compile('^\[(.*)\]-\[(.*?)\]$')
        self.opts  = self.get_config (_ADAPTOR_NAME)

    # ----------------------------------------------------------------
    #
    def sanity_check(self):
        # FIXME: also check for gsissh
        pass

    # ----------------------------------------------------------------
    #
    def parse_id(self, id):
        # split the id '[rm]-[pid]' in its parts, and return them.

        match = self.id_re.match(id)

        if not match or len(match.groups()) != 2:
            raise saga.BadParameter("Cannot parse job id '%s'" % id)

        return (match.group(1), match.group(2))


###############################################################################
#
class PBSJobService (saga.adaptors.cpi.job.Service):
    """ implements saga.adaptors.cpi.job.Service
    """

    # ----------------------------------------------------------------
    #
    def __init__(self, api, adaptor):

        self._mt  = None
        _cpi_base = super(PBSJobService, self)
        _cpi_base.__init__(api, adaptor)

        self._adaptor = adaptor

    # ----------------------------------------------------------------
    #
    def __del__(self):

        self.close()


    # ----------------------------------------------------------------
    #
    def close(self):

        if  self.mt :
            self.mt.stop()
            self.mt.join(10)  # don't block forever on join()

        self._logger.info("Job monitoring thread stopped.")

        self.finalize(True)


    # ----------------------------------------------------------------
    #
    def finalize(self, kill_shell=False):

        if  kill_shell :
            if  self.shell :
                self.shell.finalize (True)


    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def init_instance(self, adaptor_state, rm_url, session):
        """ service instance constructor
        """
        self.rm      = rm_url
        self.session = session
        self.ppn     = None
        self.is_cray = ""
        self.queue   = None
        self.shell   = None
        self.jobs    = dict()

        # the monitoring thread - one per service instance
        self.mt = _job_state_monitor(job_service=self)
        self.mt.start()

        rm_scheme = rm_url.scheme
        pty_url   = surl.Url(rm_url)

        # this adaptor supports options that can be passed via the
        # 'query' component of the job service URL.
        if rm_url.query is not None:
            for key, val in parse_qs(rm_url.query).iteritems():
                if key == 'queue':
                    self.queue = val[0]
                elif key == 'craytype':
                    self.is_cray = val[0]
                elif key == 'ppn':
                    self.ppn = int(val[0])


        # we need to extract the scheme for PTYShell. That's basically the
        # job.Service Url without the pbs+ part. We use the PTYShell to execute
        # pbs commands either locally or via gsissh or ssh.
        if rm_scheme == "pbs":
            pty_url.scheme = "fork"
        elif rm_scheme == "pbs+ssh":
            pty_url.scheme = "ssh"
        elif rm_scheme == "pbs+gsissh":
            pty_url.scheme = "gsissh"

        # these are the commands that we need in order to interact with PBS.
        # the adaptor will try to find them during initialize(self) and bail
        # out in case they are note available.
        self._commands = {'pbsnodes': None,
                          'qstat':    None,
                          'qsub':     None,
                          'qdel':     None}

        self.shell = sups.PTYShell(pty_url, self.session)

      # self.shell.set_initialize_hook(self.initialize)
      # self.shell.set_finalize_hook(self.finalize)

        self.initialize()
        return self.get_api()


    # ----------------------------------------------------------------
    #
    def initialize(self):
        # check if all required pbs tools are available
        for cmd in self._commands.keys():
            ret, out, _ = self.shell.run_sync("which %s " % cmd)
            if ret != 0:
                message = "Error finding PBS tools: %s" % out
                log_error_and_raise(message, saga.NoSuccess, self._logger)
            else:
                path = out.strip()  # strip removes newline
                if cmd == 'qdel':  # qdel doesn't support --version!
                    self._commands[cmd] = {"path":    path,
                                           "version": "?"}
                else:
                    ret, out, _ = self.shell.run_sync("%s --version" % cmd)
                    if ret != 0:
                        message = "Error finding PBS tools: %s" % out
                        log_error_and_raise(message, saga.NoSuccess,
                            self._logger)
                    else:
                        # version is reported as: "version: x.y.z"
                        version = out#.strip().split()[1]

                        # add path and version to the command dictionary
                        self._commands[cmd] = {"path":    path,
                                               "version": version}

        self._logger.info("Found PBS tools: %s" % self._commands)

        # let's try to figure out if we're working on a Cray XT machine.
        # naively, we assume that if we can find the 'aprun' command in the
        # path that we're logged in to a Cray machine.
        if self.is_cray == "":
            ret, out, _ = self.shell.run_sync('which aprun')
            if ret != 0:
                self.is_cray = ""
            else:
                self._logger.info("Host '%s' seems to be a Cray XT class machine." \
                    % self.rm.host)
                self.is_cray = "unknowncray"
        else: 
            self._logger.info("Assuming host is a Cray since 'craytype' is set to: %s" % self.is_cray)


        #
        # Get number of processes per node
        #
        if self.ppn:
            self._logger.debug("Using user specified 'ppn': %d" % self.ppn)
            return

        # TODO: this is quite a hack. however, it *seems* to work quite
        #       well in practice.
        if 'PBSPro_12' in self._commands['qstat']['version']:
            ret, out, _ = self.shell.run_sync('unset GREP_OPTIONS; %s -a | grep -E "resources_available.ncpus"' % \
                                               self._commands['pbsnodes']['path'])
        else:
            ret, out, _ = self.shell.run_sync('unset GREP_OPTIONS; %s -a | grep -E "(np|pcpu)[[:blank:]]*=" ' % \
                                               self._commands['pbsnodes']['path'])
        if ret != 0:
            message = "Error running pbsnodes: %s" % out
            log_error_and_raise(message, saga.NoSuccess, self._logger)
        else:
            # this is black magic. we just assume that the highest occurrence
            # of a specific np is the number of processors (cores) per compute
            # node. this equals max "PPN" for job scripts
            ppn_list = dict()
            for line in out.split('\n'):
                np = line.split(' = ')
                if len(np) == 2:
                    np_str = np[1].strip()
                    if np_str == '<various>':
                        continue
                    else:
                        np = int(np_str)
                    if np in ppn_list:
                        ppn_list[np] += 1
                    else:
                        ppn_list[np] = 1
            self.ppn = max(ppn_list, key=ppn_list.get)
            self._logger.debug("Found the following 'ppn' configurations: %s. "
                "Using %s as default ppn."  % (ppn_list, self.ppn))

    # ----------------------------------------------------------------
    #
    def _job_run(self, job_obj):
        """ runs a job via qsub
        """

        # get the job description
        jd = job_obj.get_description()

        # normalize working directory path
        if  jd.working_directory :
            jd.working_directory = os.path.normpath (jd.working_directory)

        if (self.queue is not None) and (jd.queue is not None):
            self._logger.warning("Job service was instantiated explicitly with \
'queue=%s', but job description tries to a different queue: '%s'. Using '%s'." %
                                (self.queue, jd.queue, self.queue))

        try:
            # create a PBS job script from SAGA job description
            script = _pbscript_generator(url=self.rm, logger=self._logger,
                                         jd=jd, ppn=self.ppn,
                                         pbs_version=self._commands['qstat']['version'],
                                         is_cray=self.is_cray, queue=self.queue,
                                         )

            self._logger.info("Generated PBS script: %s" % script)
        except Exception, ex:
            log_error_and_raise(str(ex), saga.BadParameter, self._logger)

        # try to create the working directory (if defined)
        # WARNING: this assumes a shared filesystem between login node and
        #          compute nodes.
        if jd.working_directory is not None:
            self._logger.info("Creating working directory %s" % jd.working_directory)
            ret, out, _ = self.shell.run_sync("mkdir -p %s" % (jd.working_directory))
            if ret != 0:
                # something went wrong
                message = "Couldn't create working directory - %s" % (out)
                log_error_and_raise(message, saga.NoSuccess, self._logger)

        # Now we want to execute the script. This process consists of two steps:
        # (1) we create a temporary file with 'mktemp' and write the contents of 
        #     the generated PBS script into it
        # (2) we call 'qsub <tmpfile>' to submit the script to the queueing system
        cmdline = """SCRIPTFILE=`mktemp -t SAGA-Python-PBSJobScript.XXXXXX` &&  echo "%s" > $SCRIPTFILE && %s $SCRIPTFILE && rm -f $SCRIPTFILE""" %  (script, self._commands['qsub']['path'])
        ret, out, _ = self.shell.run_sync(cmdline)

        if ret != 0:
            # something went wrong
            message = "Error running job via 'qsub': %s. Commandline was: %s" \
                % (out, cmdline)
            log_error_and_raise(message, saga.NoSuccess, self._logger)
        else:
            # parse the job id. qsub usually returns just the job id, but
            # sometimes there are a couple of lines of warnings before.
            # if that's the case, we log those as 'warnings'
            lines = out.split('\n')
            lines = filter(lambda lines: lines != '', lines)  # remove empty

            if len(lines) > 1:
                self._logger.warning('qsub: %s' % ''.join(lines[:-2]))

            # we asssume job id is in the last line
            #print cmdline
            #print out

            job_id = "[%s]-[%s]" % (self.rm, lines[-1].strip().split('.')[0])
            self._logger.info("Submitted PBS job with id: %s" % job_id)

            state = saga.job.PENDING

            # populate job info dict
            self.jobs[job_id] = {'obj'         : job_obj,
                                 'job_id'      : job_id,
                                 'state'       : state,
                                 'exec_hosts'  : None,
                                 'returncode'  : None,
                                 'create_time' : None,
                                 'start_time'  : None,
                                 'end_time'    : None,
                                 'gone'        : False
                                 }

            self._logger.info ("assign job id  %s / %s / %s to watch list (%s)" \
                            % (None, job_id, job_obj, self.jobs.keys()))

            # set status to 'pending' and manually trigger callback
            job_obj._attributes_i_set('state', state, job_obj._UP, True)

            # return the job id
            return job_id


    # ----------------------------------------------------------------
    #
    def _retrieve_job(self, job_id):
        """ see if we can get some info about a job that we don't
            know anything about
        """
        rm, pid = self._adaptor.parse_id(job_id)

        # run the PBS 'qstat' command to get some infos about our job
        if 'PBSPro_1' in self._commands['qstat']['version']:
            qstat_flag = '-f'
        else:
            qstat_flag ='-f1'

        ret, out, _ = self.shell.run_sync("unset GREP_OPTIONS; %s %s %s | \
grep -E -i '(job_state)|(exec_host)|(exit_status)|(ctime)|\
(start_time)|(comp_time)|(stime)|(qtime)|(mtime)'" % (self._commands['qstat']['path'], qstat_flag, pid))

        if ret != 0:
            message = "Couldn't reconnect to job '%s': %s" % (job_id, out)
            log_error_and_raise(message, saga.NoSuccess, self._logger)

        else:
            # the job seems to exist on the backend. let's gather some data
            job_info = {
                'job_id':       job_id,
                'state':        saga.job.UNKNOWN,
                'exec_hosts':   None,
                'returncode':   None,
                'create_time':  None,
                'start_time':   None,
                'end_time':     None,
                'gone':         False
            }

            results = out.split('\n')
            for line in results:
                if len(line.split('=')) == 2:
                    key, val = line.split('=')
                    key = key.strip()  # strip() removes whitespaces at the
                    val = val.strip()  # beginning and the end of the string

                    if key == 'job_state':
                        curr_info['state'] = _pbs_to_saga_jobstate(val)
                    elif key == 'exec_host':
                        curr_info['exec_hosts'] = val.split('+')  # format i73/7+i73/6+...
                    elif key in ['exit_status','Exit_status']:
                        curr_info['returncode'] = int(val)
                    elif key == 'ctime':
                        curr_info['create_time'] = val
                    elif key in ['start_time','stime']:
                        curr_info['start_time'] = val
                    elif key in ['comp_time','mtime']:
                        curr_info['end_time'] = val

            return job_info

    # ----------------------------------------------------------------
    #
    def _job_get_info(self, job_id):
        """ get job attributes via qstat
        """

        # if we don't have the job in our dictionary, we don't want it
        if job_id not in self.jobs:
            message = "Unknown job id: %s. Can't update state." % job_id
            log_error_and_raise(message, saga.NoSuccess, self._logger)

        # prev. info contains the info collect when _job_get_info
        # was called the last time
        prev_info = self.jobs[job_id]

        # if the 'gone' flag is set, there's no need to query the job
        # state again. it's gone forever
        if  prev_info['gone'] is True:
            return prev_info

        # curr. info will contain the new job info collect. it starts off
        # as a copy of prev_info (don't use deepcopy because there is an API 
        # object in the dict -> recursion)
        curr_info = dict()
        curr_info['obj'        ] = prev_info.get ('obj'        )
        curr_info['job_id'     ] = prev_info.get ('job_id'     )
        curr_info['state'      ] = prev_info.get ('state'      )
        curr_info['exec_hosts' ] = prev_info.get ('exec_hosts' )
        curr_info['returncode' ] = prev_info.get ('returncode' )
        curr_info['create_time'] = prev_info.get ('create_time')
        curr_info['start_time' ] = prev_info.get ('start_time' )
        curr_info['end_time'   ] = prev_info.get ('end_time'   )
        curr_info['gone'       ] = prev_info.get ('gone'       )

        rm, pid = self._adaptor.parse_id(job_id)

        # run the PBS 'qstat' command to get some infos about our job
        if 'PBSPro_1' in self._commands['qstat']['version']:
            qstat_flag = '-fx'
        else:
            qstat_flag ='-f1'
            
        ret, out, _ = self.shell.run_sync("unset GREP_OPTIONS; %s %s %s | \
grep -E -i '(job_state)|(exec_host)|(exit_status)|(ctime)|(start_time)\
|(comp_time)|(mtime)|(stime)|(qtime)|(etime)'" % (self._commands['qstat']['path'], qstat_flag, pid))

        if ret != 0:
            if ("Unknown Job Id" in out):
                # Let's see if the previous job state was runnig or pending. in
                # that case, the job is gone now, which can either mean DONE,
                # or FAILED. the only thing we can do is set it to 'DONE'
                curr_info['gone'] = True
                # we can also set the end time
                self._logger.warning("Previously running job has disappeared. This probably means that the backend doesn't store informations about finished jobs. Setting state to 'DONE'.")

                if prev_info['state'] in [saga.job.RUNNING, saga.job.PENDING]:
                    curr_info['state'] = saga.job.DONE
                else:
                    curr_info['state'] = saga.job.FAILED
            else:
                # something went wrong
                message = "Error retrieving job info via 'qstat': %s" % out
                log_error_and_raise(message, saga.NoSuccess, self._logger)
        else:
            # parse the egrep result. this should look something like this:
            #     job_state = C
            #     exec_host = i72/0
            #     exit_status = 0
            results = out.split('\n')
            for result in results:
                if len(result.split('=')) == 2:
                    key, val = result.split('=')
                    key = key.strip()  # strip() removes whitespaces at the
                    val = val.strip()  # beginning and the end of the string

                    if key == 'job_state':
                        curr_info['state'] = _pbs_to_saga_jobstate(val)
                    elif key == 'exec_host':
                        curr_info['exec_hosts'] = val.split('+')  # format i73/7+i73/6+...
                    elif key in ['exit_status','Exit_status']:
                        curr_info['returncode'] = int(val)
                    elif key == 'ctime':
                        curr_info['create_time'] = val
                    elif key in ['start_time','stime']:
                        curr_info['start_time'] = val
                    elif key in ['comp_time','mtime']:
                        curr_info['end_time'] = val

        # return the new job info dict
        return curr_info

    # ----------------------------------------------------------------
    #
    def _job_get_state(self, job_id):
        """ get the job's state
        """
        return self.jobs[job_id]['state']

    # ----------------------------------------------------------------
    #
    def _job_get_exit_code(self, job_id):
        """ get the job's exit code
        """
        ret = self.jobs[job_id]['returncode']

        # FIXME: 'None' should cause an exception
        if ret == None : return None
        else           : return int(ret)

    # ----------------------------------------------------------------
    #
    def _job_get_execution_hosts(self, job_id):
        """ get the job's exit code
        """
        return self.jobs[job_id]['exec_hosts']

    # ----------------------------------------------------------------
    #
    def _job_get_create_time(self, job_id):
        """ get the job's creation time
        """
        return self.jobs[job_id]['create_time']

    # ----------------------------------------------------------------
    #
    def _job_get_start_time(self, job_id):
        """ get the job's start time
        """
        return self.jobs[job_id]['start_time']

    # ----------------------------------------------------------------
    #
    def _job_get_end_time(self, job_id):
        """ get the job's end time
        """
        return self.jobs[job_id]['end_time']

    # ----------------------------------------------------------------
    #
    def _job_cancel(self, job_id):
        """ cancel the job via 'qdel'
        """
        rm, pid = self._adaptor.parse_id(job_id)

        ret, out, _ = self.shell.run_sync("%s %s\n" \
            % (self._commands['qdel']['path'], pid))

        if ret != 0:
            message = "Error canceling job via 'qdel': %s" % out
            log_error_and_raise(message, saga.NoSuccess, self._logger)

        # assume the job was succesfully canceled
        self.jobs[job_id]['state'] = saga.job.CANCELED


    # ----------------------------------------------------------------
    #
    def _job_wait(self, job_id, timeout):
        """ wait for the job to finish or fail
        """
        time_start = time.time()
        time_now   = time_start
        rm, pid    = self._adaptor.parse_id(job_id)

        while True:
            state = self.jobs[job_id]['state']  # this gets updated in the bg.

            if state == saga.job.DONE or \
               state == saga.job.FAILED or \
               state == saga.job.CANCELED:
                    return True

            # avoid busy poll
            time.sleep(SYNC_WAIT_UPDATE_INTERVAL)

            # check if we hit timeout
            if timeout >= 0:
                time_now = time.time()
                if time_now - time_start > timeout:
                    return False

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def create_job(self, jd):
        """ implements saga.adaptors.cpi.job.Service.get_url()
        """
        # this dict is passed on to the job adaptor class -- use it to pass any
        # state information you need there.
        adaptor_state = {"job_service":     self,
                         "job_description": jd,
                         "job_schema":      self.rm.schema,
                         "reconnect":       False
                         }

        # create and return a new job object
        return saga.job.Job(_adaptor=self._adaptor,
                            _adaptor_state=adaptor_state)

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_job(self, job_id):
        """ Implements saga.adaptors.cpi.job.Service.get_job()
        """

      # self._logger.info("checking watch list for %s" % job_id)

        if  job_id in self.jobs :

      #     self._logger.info("checking watch list for %s - found" % job_id)
            return self.jobs[job_id]['obj']

      # else :
      #     self._logger.info("checking watch list for %s - not found" % job_id)


        # try to get some information about this job
        job_info = self._retrieve_job(job_id)

        # this dict is passed on to the job adaptor class -- use it to pass any
        # state information you need there.
        adaptor_state = {"job_service":     self,
                         # TODO: fill job description
                         "job_description": saga.job.Description(),
                         "job_schema":      self.rm.schema,
                         "reconnect":       True,
                         "reconnect_jobid": job_id
                         }

        job_obj = saga.job.Job(_adaptor=self._adaptor,
                               _adaptor_state=adaptor_state)

      # self._logger.info("adding     job %s / %s to watch list (%s)" % (job_id, job_obj, self.jobs.keys()))

        # throw it into our job dictionary.
        job_info['obj']   = job_obj
        self.jobs[job_id] = job_info

        return job_obj

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_url(self):
        """ implements saga.adaptors.cpi.job.Service.get_url()
        """
        return self.rm

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def list(self):
        """ implements saga.adaptors.cpi.job.Service.list()
        """
        ids = []

        ret, out, _ = self.shell.run_sync("unset GREP_OPTIONS; %s | grep `whoami`" %
                                          self._commands['qstat']['path'])

        if ret != 0 and len(out) > 0:
            message = "failed to list jobs via 'qstat': %s" % out
            log_error_and_raise(message, saga.NoSuccess, self._logger)
        elif ret != 0 and len(out) == 0:
            # qstat | grep `` exits with 1 if the list is empty
            pass
        else:
            for line in out.split("\n"):
                # output looks like this:
                # 112059.svc.uc.futuregrid testjob oweidner 0 Q batch
                # 112061.svc.uc.futuregrid testjob oweidner 0 Q batch
                if len(line.split()) > 1:
                    job_id = "[%s]-[%s]" % (self.rm, line.split()[0].split('.')[0])
                    ids.append(str(job_id))

        return ids


  # # ----------------------------------------------------------------
  # #
  # def container_run (self, jobs) :
  #     self._logger.debug ("container run: %s"  %  str(jobs))
  #     # TODO: this is not optimized yet
  #     for job in jobs:
  #         job.run ()
  #
  #
  # # ----------------------------------------------------------------
  # #
  # def container_wait (self, jobs, mode, timeout) :
  #     self._logger.debug ("container wait: %s"  %  str(jobs))
  #     # TODO: this is not optimized yet
  #     for job in jobs:
  #         job.wait ()
  #
  #
  # # ----------------------------------------------------------------
  # #
  # def container_cancel (self, jobs) :
  #     self._logger.debug ("container cancel: %s"  %  str(jobs))
  #     raise saga.NoSuccess ("Not Implemented");


###############################################################################
#
class PBSJob (saga.adaptors.cpi.job.Job):
    """ implements saga.adaptors.cpi.job.Job
    """

    def __init__(self, api, adaptor):

        # initialize parent class
        _cpi_base = super(PBSJob, self)
        _cpi_base.__init__(api, adaptor)

    def _get_impl(self):
        return self

    @SYNC_CALL
    def init_instance(self, job_info):
        """ implements saga.adaptors.cpi.job.Job.init_instance()
        """
        # init_instance is called for every new saga.job.Job object
        # that is created
        self.jd = job_info["job_description"]
        self.js = job_info["job_service"]

        if job_info['reconnect'] is True:
            self._id      = job_info['reconnect_jobid']
            self._started = True
        else:
            self._id      = None
            self._started = False

        return self.get_api()

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_state(self):
        """ implements saga.adaptors.cpi.job.Job.get_state()
        """
        if  self._started is False:
            return saga.job.NEW

        return self.js._job_get_state(job_id=self._id)
            
    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def wait(self, timeout):
        """ implements saga.adaptors.cpi.job.Job.wait()
        """
        if self._started is False:
            log_error_and_raise("Can't wait for job that hasn't been started",
                saga.IncorrectState, self._logger)
        else:
            self.js._job_wait(job_id=self._id, timeout=timeout)

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def cancel(self, timeout):
        """ implements saga.adaptors.cpi.job.Job.cancel()
        """
        if self._started is False:
            log_error_and_raise("Can't wait for job that hasn't been started",
                saga.IncorrectState, self._logger)
        else:
            self.js._job_cancel(self._id)

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def run(self):
        """ implements saga.adaptors.cpi.job.Job.run()
        """
        self._id = self.js._job_run(self._api())
        self._started = True

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_service_url(self):
        """ implements saga.adaptors.cpi.job.Job.get_service_url()
        """
        return self.js.rm

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_id(self):
        """ implements saga.adaptors.cpi.job.Job.get_id()
        """
        return self._id

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_exit_code(self):
        """ implements saga.adaptors.cpi.job.Job.get_exit_code()
        """
        if self._started is False:
            return None
        else:
            return self.js._job_get_exit_code(self._id)

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_created(self):
        """ implements saga.adaptors.cpi.job.Job.get_created()
        """
        if self._started is False:
            return None
        else:
            return self.js._job_get_create_time(self._id)

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_started(self):
        """ implements saga.adaptors.cpi.job.Job.get_started()
        """
        if self._started is False:
            return None
        else:
            return self.js._job_get_start_time(self._id)

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_finished(self):
        """ implements saga.adaptors.cpi.job.Job.get_finished()
        """
        if self._started is False:
            return None
        else:
            return self.js._job_get_end_time(self._id)

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_execution_hosts(self):
        """ implements saga.adaptors.cpi.job.Job.get_execution_hosts()
        """
        if self._started is False:
            return None
        else:
            return self.js._job_get_execution_hosts(self._id)

    # ----------------------------------------------------------------
    #
    @SYNC_CALL
    def get_description(self):
        """ implements saga.adaptors.cpi.job.Job.get_execution_hosts()
        """
        return self.jd


