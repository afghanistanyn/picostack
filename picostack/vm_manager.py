import os
import signal
import shutil
import logging
import psutil
from collections import deque
from picostack.textwrap_util import wrap_multiline
from picostack.vms.models import (
    VmInstance, VM_PORTS,
    VM_IN_CLONING, VM_IS_STOPPED, VM_IS_LAUNCHED, VM_IS_RUNNING,
    VM_HAS_FAILED, VM_IS_TERMINATING, VM_IS_TRASHED,
)
from process_spawn import ProcessUtil

logger = logging.getLogger(__name__)


class CallBuilder(object):
    '''
    Many host systems differ in a way the run kvm. This helper class provides
    generic workarounds for construction of command line parameters according
    to such differences.
    '''
    def __init__(self):
        self.executable = None
        self.parameters = dict()
        self.configure()

    @classmethod
    def factory(cls, builder_name):
        if builder_name == 'ubuntu_kvm':
            return UbuntuKvm()
        elif builder_name == 'debian_kvm':
            return DebianKvm()
        raise Exception('Unknown call builder name: %s' % builder_name)

    def build_params(self):
        options = list()
        for key in self.parameters:
            value = self.parameters[key]
            if type(value) == list:
                for subvalue in value:
                    options.append('-' + key + ' ' + subvalue)
            else:
                options.append('-' + key + ' ' + value)
        return ' '.join(options)

    def get_call(self, substitute_vars):
        '''Make a command line text with VM call.'''
        return self.executable + ' ' + self.build_params() % substitute_vars

    def configure(self):
        '''Configure command line builder with default set of parameters.'''
        self.parameters['machine'] = 'accel=kvm'
        self.parameters['hda'] = '%(disk_path)s'
        self.parameters['boot'] = 'c'
        self.parameters['m'] = '%(memory_size)s'
        self.parameters['cpu'] = 'qemu64'
        self.parameters['smp'] = '%(num_of_cores)s,cores=%(num_of_cores)s,'\
            + 'sockets=1,threads=1'
        self.parameters['net'] = ['user', 'nic,model=virtio']
        self.parameters['usbdevice'] = 'tablet'
        self.parameters['no-shutdown'] = ''


class UbuntuKvm(CallBuilder):

    def __init__(self):
        CallBuilder.__init__(self)
        self.executable = '/usr/bin/kvm'

    def configure(self):
        CallBuilder.configure(self)
        # Continue with ubuntu scenario
        self.parameters['net'].append('nic,model=virtio')
        self.parameters['balloon'] = 'virtio'


class DebianKvm(CallBuilder):

    def configure(self):
        CallBuilder.configure(self)
        # change executable
        self.executable = '/usr/bin/qemu-system-x86_64 -enable-kvm'
        # Continue with debian scenario
        self.parameters['net'].append('nic,model=virtio')
        self.parameters['balloon'] = 'virtio'


class VmManager(object):

    def __init__(self, config):
        self.config = config
        self.__next_unmapped_port = None
        self.call_builder = CallBuilder.factory(self.call_builder_name)

    @property
    def call_builder_name(self):
        if self.config.has_option('vm_manager', 'call_builder'):
            return self.config.get('vm_manager', 'call_builder')
        return 'ubuntu_kvm'

    @property
    def vm_image_path(self):
        return self.config.get('vm_manager', 'vm_image_path')

    @property
    def vm_disk_path(self):
        return self.config.get('vm_manager', 'vm_disk_path')

    def validate_config(self):
        assert self.config.has_section('vm_manager')
        assert os.path.exists(self.vm_image_path)
        assert os.path.exists(self.vm_disk_path)

    @property
    def mapping_port_range(self):
        first_port = int(self.config.get('app', 'first_mapped_port'))
        last_port = int(self.config.get('app', 'last_mapped_port'))
        assert last_port > first_port
        if self.__next_unmapped_port is None \
                or self.__next_unmapped_port > last_port:
            self.__next_unmapped_port = first_port
        mapping_port_range = range(first_port, last_port)
        result = deque(mapping_port_range)
        shift = self.__next_unmapped_port - first_port + 1
        result.rotate(-1 * shift)
        return result

    def get_next_unmapped_port(self):
        '''
        Get a next port form the mapping range that was not mapped by any
        instances.
        '''
        # Get a list of ports, occupied by running instances
        already_mapped_ports = VmInstance.get_all_occupied_ports()
        # Continue until unmapped port is found.
        for next_port in self.mapping_port_range:
            if next_port in already_mapped_ports:
                continue
            # Found unmapped port.
            self.__next_unmapped_port = next_port
            return next_port
        raise Exception('Failed to find unmapped/unoccupied port.')

    @property
    def location_of_images(self):
        return self.config.get('vm_manager', 'vm_image_path')

    def get_image_path(self, image):
        return os.path.join(self.location_of_images, image.image_filename)

    @property
    def location_of_disks(self):
        return self.config.get('vm_manager', 'vm_disk_path')

    def get_disk_path(self, machine):
        return os.path.join(self.location_of_disks, machine.disk_filename)

    def get_pid_file(self, machine):
        pidfiles_folder = self.config.get('app', 'pidfiles_path')
        return os.path.join(pidfiles_folder, '%s.pid' % machine.name)

    def get_report_file(self, machine):
        logfiles_folder = self.config.get('app', 'log_path')
        return os.path.join(logfiles_folder, '%s.log' % machine.name)

    @classmethod
    def create(self, name, config):
        '''Fabric of VM managers'''
        if name.upper() == 'KVM':
            return Kvm(config)
        raise Exception('Unknown VM manager: %s' % name)

    def build_machines(self):
        instances = VmInstance.objects.filter(current_state=VM_IN_CLONING)
        if not instances.exists():
            logger.info('Nothing to clone..')
            return
        for machine in instances:
            logger.info('Cloning "%s"' % machine.name)
            self.clone_from_image(machine)

    def start_machines(self):
        instances = VmInstance.objects.filter(current_state=VM_IS_LAUNCHED)
        if not instances.exists():
            logger.info('Nothing to start..')
            return
        for machine in instances:
            logger.info('Start running machine "%s"' % machine.name)
            self.run_machine(machine)

    def stop_machines(self):
        instances = VmInstance.objects.filter(current_state=VM_IS_TERMINATING)
        if not instances.exists():
            logger.info('Nothing to stop..')
            return
        for machine in instances:
            logger.info('Terminating machine "%s"' % machine.name)
            self.stop_machine(machine)

    def destory_machines(self):
        instances = VmInstance.objects.filter(current_state=VM_IS_TRASHED)
        if not instances.exists():
            logger.info('Nothing to trash..')
            return
        for machine in instances:
            logger.info('Trashing machine "%s"' % machine.name)
            self.remove_machine(machine)

    def run_machine(self, machine):
        raise NotImplementedError()

    def stop_machine(self, machine):
        raise NotImplementedError()

    def clone_from_image(self, machine):
        raise NotImplementedError()

    def remove_machine(self, vm_image):
        raise NotImplementedError()

    def kill_all_machines(self):
        raise NotImplementedError()


class Kvm(VmManager):

    def get_kvm_call(self, machine):
        # Make a list of ports to redirect from the VM to host. Ports will be
        # available at the host computer.
        redirected_ports = ''
        ports_to_map = list()
        if machine.has_ssh:
            ports_to_map.append('ssh')
        if machine.has_vnc:
            # TODO: check if VNC should be a "redirected port"?
            ports_to_map.append('vnc')
        if machine.has_rdp:
            ports_to_map.append('rdp')
        for port_to_map in ports_to_map:
            unmapped_port = self.get_next_unmapped_port()
            machine.map_port(port_to_map, unmapped_port)
            redirected_ports += ' -redir tcp:%d::%d ' % (unmapped_port,
                                                         VM_PORTS[port_to_map])
        host_vnc = '-vnc localhost:%d' % machine.localhost_vnc_port
        # Make a command line text with KVM call.
        return self.call_builder.get_call({
            'disk_path': self.get_disk_path(machine),
            'memory_size': machine.memory_size,
            'num_of_cores': machine.num_of_cores,
            'redirected_ports': redirected_ports,
            'host_vnc': host_vnc,
        }) + ' '.join([redirected_ports, host_vnc])

    def run_machine(self, machine):
        # Check if machine is in accepting state.
        assert machine.current_state == VM_IS_LAUNCHED
        # Bake a shell command to spawn the machine.
        shell_command = self.get_kvm_call(machine)
        logger.debug('Running VM with shell command:\n%s' % shell_command)
        #output = invoke(command)
        report_filepath = self.get_report_file(machine)
        pid_filepath = self.get_pid_file(machine)
        if ProcessUtil.process_runs(pid_filepath):
            logging.warning('Apparently, VM process is already running. '
                            'Check %s ' % pid_filepath)
            machine.change_state(VM_HAS_FAILED)
            # TODO: kill the VM?
            return
        ProcessUtil.exec_process(shell_command, report_filepath, pid_filepath)
        # Update state.
        machine.change_state(VM_IS_RUNNING)

    def stop_machine(self, machine):
        # Check if machine is in accepting state.
        assert machine.current_state == VM_IS_TERMINATING
        # Kill the machine by pid.
        cxt_pidfile_filepath = self.get_pid_file(machine)
        proc_pidfile_path = '%s_proc' % cxt_pidfile_filepath
        # First kill proc which is a child and then daemoncxt.
        if ProcessUtil.kill_process(proc_pidfile_path) \
                and ProcessUtil.kill_process(cxt_pidfile_filepath):
            logging.info('Successfully stopping VM processes as in %s and %s' %
                         (proc_pidfile_path, cxt_pidfile_filepath))
            # Proc pid should be taken care of.
            if os.path.exists(proc_pidfile_path):
                os.unlink(proc_pidfile_path)
        else:
            logging.warning('Expected VM process does not run anymore. '
                            'Please check the log file for details: %s' %
                            self.get_report_file(machine))
        # Update state.
        machine.change_state(VM_IS_STOPPED)

    def clone_from_image(self, machine):
        # Check if machine is in accepting state.
        assert machine.current_state == VM_IN_CLONING
        logger.info('Cloning new machine \'%s\' form image \'%s\'' %
                    (machine.name, machine.image.name))
        # Copy machine. Can take time.
        src_file = self.get_image_path(machine.image)
        dst_file = self.get_disk_path(machine)
        logger.info('Copying %s -> %s' %
                    (src_file, dst_file))
        shutil.copyfile(src_file, dst_file)
        # Update state to VM_IS_STOPPED - we are ready to run.
        machine.change_state(VM_IS_STOPPED)

    def remove_machine(self, machine):
        # Check if machine is in accepting state.
        assert machine.current_state == VM_IS_TRASHED
        logger.info('Removing trashed machine \'%s\' and its files: \'%s\'' %
                    (machine.name, machine.disk_filename))
        disk_file = self.get_disk_path(machine)
        try:
            os.unlink(disk_file)
        except IOError:
            logger.info('Failed to remove the VM\'s disk: %s' % disk_file,
                        exc_info=True)
        # Clean logs.
        report_filepath = self.get_report_file(machine)
        if os.path.exists(report_filepath):
            os.unlink(report_filepath)
        # Finally kill the DB record.
        machine.delete()

    def kill_all_machines(self):
        needle = self.vm_disk_path
        machines = list()
        for proc in psutil.process_iter():
            try:
                pinfo = proc.as_dict(attrs=['pid', 'cmdline'])
            except psutil.NoSuchProcess:
                pass
            else:
                if needle in ' '.join(pinfo['cmdline']):
                    machines.append(pinfo)
        for machine in machines:
            os.kill(machine['pid'], signal.SIGTERM)

