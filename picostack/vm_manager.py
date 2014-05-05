import os
import shutil
import logging
import textwrap
from subprocess import (PIPE, Popen)
from picostack.vms.models import VmInstance, VM_PORTS
from process_spawn import ProcessUtil

logger = logging.getLogger('picostack.application')


def invoke(command, _in=None):
    '''
    Invoke command as a new system process and return its output.
    '''
    process = Popen(command, stdin=PIPE, stdout=PIPE, shell=True,
                    executable='/bin/bash')
    if _in is not None:
        process.stdin.write(_in)
    return process.stdout.read()


class VmManager(object):

    def __init__(self, config):
        self.config = config
        self.__mapping_port_range = None

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
        if self.__mapping_port_range is None:
            first_port = int(self.config.get('app', 'first_mapped_port'))
            last_port = int(self.config.get('app', 'last_mapped_port'))
            assert last_port > first_port
            self.__mapping_port_range = range(first_port, last_port)
        return self.__mapping_port_range

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
        return os.path.join(pidfiles_folder, '%s.log' % machine.name)

    @classmethod
    def create(self, name, config):
        '''Fabric of VM managers'''
        if name.upper() == 'KVM':
            return Kvm(config)
        raise Exception('Unknown VM manager: %s' % name)

    def build_machines(self):
        for machine in VmInstance.objects.filter(current_state='InCloning'):
            self.clone_from_image(machine)

    def start_machines(self):
        for machine in VmInstance.objects.filter(current_state='Launched'):
            self.run_machine(machine)

    def stop_machines(self):
        for machine in VmInstance.objects.filter(current_state='Terminating'):
            self.stop_machine(machine)

    def destory_machines(self):
        for machine in VmInstance.objects.filter(current_state='Trashed'):
            self.remove_machine(machine)

    def run_machine(self, machine):
        raise NotImplementedError()

    def stop_machine(self, machine):
        raise NotImplementedError()

    def clone_from_image(self, machine):
        raise NotImplementedError()

    def remove_machine(self, vm_image):
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
        # Make a command line text with KVM call.
        command_lines = textwrap.wrap('''
            sudo /usr/bin/kvm -machine accel=kvm -hda %(image_path)s
                -boot c
                -m %(memory_size)s
                -cpu qemu64 -smp %(num_of_cores)s,cores=11,sockets=1,threads=1
                -net user -net nic,model=virtio
                %(redirected_ports)s
                -usbdevice tablet
                -vnc localhost:1
        ''' % {
            'image_path': machine.get_image_path(),
            'memory_size': machine.memory_size,
            'num_of_cores': machine.num_of_cores,
            'redirected_ports': redirected_ports,
        }, width=210, break_on_hyphens=False, break_long_words=False)
        command = ' \\\n'.join(command_lines)
        return command

    def run_machine(self, machine):
        # Check if machine is in accepting state.
        assert machine.current_state == 'Stopped'
        # Bake a shell command to spawn the machine.
        shell_command = self.get_kvm_call(machine)
        logger.debug('Running VM with shell command:\n%s' % shell_command)
        #output = invoke(command)
        report_filepath = self.get_report_file(prefix=machine.name)
        pid_filepath = self.get_pid_file(machine)
        assert not ProcessUtil.process_runs(pid_filepath)
        ProcessUtil.exec_process(shell_command, report_filepath, pid_filepath)

    def stop_machine(self, machine):
        # Check if machine is in accepting state.
        assert machine.current_state == 'Running'
        # Kill the machine by pid.
        pid_filepath = self.get_pid_file(machine)
        ProcessUtil.kill_process(pid_filepath)
        # Update state.
        machine.change_state('Stopped')

    def clone_from_image(self, machine):
        # Check if machine is in accepting state.
        assert machine.current_state == 'InCloning'
        logger.info('Cloning new machine \'%s\' form image \'%s\'' %
                    (machine.name, machine.image.name))
        # Copy machine. Can take time.
        src_file = self.get_image_path(machine.image)
        dst_file = self.get_disk_path(machine)
        logger.info('Copying %s -> %s' %
                    (src_file, dst_file))
        shutil.copyfile(src_file, dst_file)
        # Update state to 'Stopped' - we are ready to run.
        machine.change_state('Stopped')

    def remove_machine(self, machine):
        # Check if machine is in accepting state.
        assert machine.current_state == 'Trashed'
        logger.info('Removing trashed machine \'%s\' and its files: \'%s\'' %
                    (machine.name, machine.disk_filename))
        disk_file = self.get_disk_path(machine)
        os.unlink(disk_file)
        machine.delete()
        # TODO: clean logs?
