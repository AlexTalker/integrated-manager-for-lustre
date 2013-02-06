from chroma_core.services.plugin_runner.resource_manager import ResourceManager
from django.db import connection
from django.db.models.query_utils import Q
from chroma_core.lib.util import dbperf
from chroma_core.models.host import Volume, VolumeNode, ManagedHost
from chroma_core.models.storage_plugin import StorageResourceRecord
from helper import load_plugins
from tests.unit.chroma_core.helper import JobTestCaseWithHost, MockAgentRpc


class ResourceManagerTestCase(JobTestCaseWithHost):
    def setUp(self):
        super(ResourceManagerTestCase, self).setUp()

        self.manager = load_plugins([
            'example_plugin',
            'linux',
            'subscription_plugin',
            'virtual_machine_plugin',
            'alert_plugin'])

        import chroma_core.lib.storage_plugin.manager
        self.old_manager = chroma_core.lib.storage_plugin.manager.storage_plugin_manager
        chroma_core.lib.storage_plugin.manager.storage_plugin_manager = self.manager

    def tearDown(self):
        import chroma_core.lib.storage_plugin.manager
        chroma_core.lib.storage_plugin.manager.storage_plugin_manager = self.old_manager

        super(ResourceManagerTestCase, self).tearDown()

    def __init__(self, *args, **kwargs):
        self._handle_counter = 0
        super(ResourceManagerTestCase, self).__init__(*args, **kwargs)

    def _get_handle(self):
        self._handle_counter += 1
        return self._handle_counter

    def _make_local_resource(self, plugin_name, class_name, **kwargs):
        from chroma_core.lib.storage_plugin.manager import storage_plugin_manager

        klass, klass_id = storage_plugin_manager.get_plugin_resource_class(plugin_name, class_name)
        resource = klass(**kwargs)
        resource.validate()
        resource._handle = self._get_handle()
        resource._handle_global = False

        return resource

    def _make_global_resource(self, plugin_name, class_name, attrs):
        from chroma_core.lib.storage_plugin.manager import storage_plugin_manager
        resource_class, resource_class_id = storage_plugin_manager.get_plugin_resource_class(plugin_name, class_name)
        resource_record, created = StorageResourceRecord.get_or_create_root(resource_class, resource_class_id, attrs)
        resource = resource_record.to_resource()
        resource._handle = self._get_handle()
        resource._handle_global = False
        return resource_record, resource


class TestSessions(ResourceManagerTestCase):
    def setUp(self):
        super(TestSessions, self).setUp()

        resource_class, resource_class_id = self.manager.get_plugin_resource_class('example_plugin', 'Couplet')
        record, created = StorageResourceRecord.get_or_create_root(resource_class, resource_class_id, {
            'address_1': '192.168.0.1', 'address_2': '192.168.0.2'})

        self.scannable_resource_id = record.pk

    def test_open_close(self):
        resource_manager = ResourceManager()
        self.assertEqual(len(resource_manager._sessions), 0)

        # Create a new session (clean slate)
        resource_manager.session_open(self.scannable_resource_id, [], 60)
        self.assertEqual(len(resource_manager._sessions), 1)

        # Create a new session (override previous)
        resource_manager.session_open(self.scannable_resource_id, [], 60)
        self.assertEqual(len(resource_manager._sessions), 1)

        # Close a session
        resource_manager.session_close(self.scannable_resource_id)
        self.assertEqual(len(resource_manager._sessions), 0)

        # Check that it's allowed to close a non-existent session
        # (plugins don't have to guarantee opening before calling
        # closing in a finally block)
        resource_manager.session_close(self.scannable_resource_id)
        self.assertEqual(len(resource_manager._sessions), 0)


class TestManyObjects(ResourceManagerTestCase):
    def setUp(self):
        super(TestManyObjects, self).setUp()

        resource_record, scannable_resource = self._make_global_resource('linux', 'PluginAgentResources',
                {'plugin_name': 'linux', 'host_id': self.host.id})

        couplet_record, couplet_resource = self._make_global_resource('example_plugin', 'Couplet',
                {'address_1': 'foo', 'address_2': 'bar'})

        self.host_resource_pk = resource_record.pk
        self.couplet_resource_pk = couplet_record.pk

        # luns
        self.N = 32
        # drives per lun
        self.M = 10
        self.host_resources = [scannable_resource]
        self.controller_resources = [couplet_resource]
        lun_size = 1024 * 1024 * 1024 * 73
        for n in range(0, self.N):
            drives = []
            for m in range(0, self.M):
                drive_resource = self._make_local_resource('example_plugin', 'HardDrive', serial_number = "foobarbaz%s_%s" % (n, m), capacity = lun_size / self.M)
                drives.append(drive_resource)
            lun_resource = self._make_local_resource(
                'example_plugin', 'Lun',
                parents = drives,
                serial_83 = "foobar%d" % n,
                local_id = n,
                size = lun_size,
                name = "LUN_%d" % n)
            self.controller_resources.extend(drives + [lun_resource])

            dev_resource = self._make_local_resource('linux', 'ScsiDevice',
                serial_80 = None,
                serial_83 = "foobar%d" % n,
                size = lun_size)
            node_resource = self._make_local_resource('linux', 'LinuxDeviceNode', path = "/dev/foo%s" % n, parents = [dev_resource], host_id = self.host.id)
            self.host_resources.extend([dev_resource, node_resource])

    def test_global_remove(self):
        try:
            dbperf.enabled = True
            connection.use_debug_cursor = True

            resource_manager = ResourceManager()

            with dbperf('session_open_host'):
                resource_manager.session_open(self.host_resource_pk, self.host_resources, 60)
            with dbperf('session_open_controller'):
                resource_manager.session_open(self.couplet_resource_pk, self.controller_resources, 60)

            host_res_count = self.N * 2 + 1
            cont_res_count = self.N + self.N * self.M + 1
            self.assertEqual(StorageResourceRecord.objects.count(), host_res_count + cont_res_count)
            self.assertEqual(Volume.objects.count(), self.N)
            self.assertEqual(VolumeNode.objects.count(), self.N)

            with dbperf('global_remove_resource_host'):
                resource_manager.global_remove_resource(self.host_resource_pk)

            self.assertEqual(StorageResourceRecord.objects.count(), cont_res_count)

            with dbperf('global_remove_resource_controller'):
                resource_manager.global_remove_resource(self.couplet_resource_pk)

            self.assertEqual(StorageResourceRecord.objects.count(), 0)

        finally:
            dbperf.enabled = False
            connection.use_debug_cursor = False


class TestVirtualMachines(ResourceManagerTestCase):
    def setUp(self):
        super(TestVirtualMachines, self).setUp()
        self.mock_servers = {'myvm': {
            'fqdn': 'myvm.mycompany.com',
            'nodename': 'test01.myvm.mycompany.com',
            'nids': ["192.168.0.19@tcp"]
        }}
        MockAgentRpc.mock_servers = self.mock_servers

    def test_virtual_machine_initial(self):
        """Check that ManagedHosts are created for VirtualMachines when
        present in the initial resource set"""
        controller_record, controller_resource = self._make_global_resource('virtual_machine_plugin', 'Controller', {'address': '192.168.0.1'})
        vm_resource = self._make_local_resource('virtual_machine_plugin', 'VirtualMachine', address = 'myvm')

        self.assertEqual(ManagedHost.objects.count(), 1)

        resource_manager = ResourceManager()
        # Session for host resources
        resource_manager.session_open(controller_record.pk, [controller_resource, vm_resource], 60)
        self.assertEqual(ManagedHost.objects.count(), 2)

    def test_virtual_machine_update(self):
        """Check that ManagedHosts are created for VirtualMachines when
        added in an update"""
        controller_record, controller_resource = self._make_global_resource('virtual_machine_plugin', 'Controller',
            {'address': '192.168.0.1'})
        vm_resource = self._make_local_resource('virtual_machine_plugin', 'VirtualMachine',
            address = 'myvm')

        resource_manager = ResourceManager()
        # Session for host resources
        resource_manager.session_open(controller_record.pk, [controller_resource], 60)
        self.assertEqual(ManagedHost.objects.count(), 1)
        resource_manager.session_add_resources(controller_record.pk, [controller_resource, vm_resource])
        self.assertEqual(ManagedHost.objects.count(), 2)

    def test_virtual_machine_existing(self):
        """Check that a virtual machine with the same address
        as an existing host gets linked to that host"""
        controller_record, controller_resource = self._make_global_resource('virtual_machine_plugin', 'Controller', {'address': '192.168.0.1'})
        vm_resource = self._make_local_resource('virtual_machine_plugin', 'VirtualMachine', address = 'myaddress')

        resource_manager = ResourceManager()
        # Session for host resources
        self.assertEqual(ManagedHost.objects.count(), 1)
        resource_manager.session_open(controller_record.pk, [controller_resource, vm_resource], 60)
        self.assertEqual(ManagedHost.objects.count(), 1)


class TestResourceOperations(ResourceManagerTestCase):
    def setUp(self):
        super(TestResourceOperations, self).setUp()

        resource_record, scannable_resource = self._make_global_resource('linux', 'PluginAgentResources', {'plugin_name': 'linux', 'host_id': self.host.id})

        self.scannable_resource_pk = resource_record.pk
        self.scannable_resource = scannable_resource

        self.dev_resource = self._make_local_resource('linux', 'ScsiDevice', serial_80 = "foobar", serial_83 = None, size = 4096)
        self.node_resource = self._make_local_resource('linux', 'LinuxDeviceNode', path = "/dev/foo", parents = [self.dev_resource], host_id = self.host.id)

    def test_re_add(self):
        resource_manager = ResourceManager()
        resource_manager.session_open(self.scannable_resource_pk, [self.scannable_resource, self.dev_resource, self.node_resource], 60)

        self.assertEqual(StorageResourceRecord.objects.count(), 3)
        resource_manager.session_remove_resources(self.scannable_resource_pk, [self.node_resource])
        self.assertEqual(StorageResourceRecord.objects.count(), 2)
        resource_manager.session_add_resources(self.scannable_resource_pk, [self.node_resource])
        self.assertEqual(StorageResourceRecord.objects.count(), 3)

    def test_global_remove(self):
        resource_manager = ResourceManager()
        resource_manager.session_open(self.scannable_resource_pk, [self.scannable_resource, self.dev_resource, self.node_resource], 60)
        self.assertEqual(StorageResourceRecord.objects.count(), 3)
        resource_manager.global_remove_resource(self.scannable_resource_pk)
        self.assertEqual(StorageResourceRecord.objects.count(), 0)

    def test_reference(self):
        """Create and save a resource which uses attributes.ResourceReference"""
        partition = self._make_local_resource('linux', 'Partition',
                                              container = self.dev_resource, number = 0, size = 1024 * 1024 * 500)

        resource_manager = ResourceManager()
        resource_manager.session_open(self.scannable_resource_pk, [
            self.scannable_resource, partition, self.dev_resource, self.node_resource], 60)

        from chroma_core.models import StorageResourceAttributeReference
        self.assertEqual(StorageResourceAttributeReference.objects.count(), 1)
        self.assertNotEqual(StorageResourceAttributeReference.objects.get().value, None)

    def test_subscriber(self):
        """Create a pair of resources where one subscribes to the other"""
        controller_record, controller_resource = self._make_global_resource('subscription_plugin', 'Controller', {'address': '192.168.0.1'})
        lun_resource = self._make_local_resource('subscription_plugin', 'Lun', lun_id = 'foobar', size = 1024 * 1024)
        presentation_resource = self._make_local_resource('subscription_plugin', 'Presentation', host_id = self.host.id, path = '/dev/foo', lun_id = 'foobar')

        resource_manager = ResourceManager()
        # Session for host resources
        resource_manager.session_open(self.scannable_resource_pk, [self.scannable_resource, self.dev_resource, self.node_resource], 60)

        # Session for controller resources
        resource_manager.session_open(controller_record.pk, [controller_resource, lun_resource, presentation_resource], 60)

        # Check relations created
        node_klass, node_klass_id = self.manager.get_plugin_resource_class('linux', 'LinuxDeviceNode')
        presentation_klass, presentation_klass_id = self.manager.get_plugin_resource_class('subscription_plugin', 'Presentation')
        lun_klass, lun_klass_id = self.manager.get_plugin_resource_class('subscription_plugin', 'Lun')
        records = StorageResourceRecord.objects.all()
        for r in records:
            resource = r.to_resource()
            parent_resources = [pr.to_resource().__class__ for pr in r.parents.all()]

            if isinstance(resource, node_klass):
                self.assertIn(presentation_klass, parent_resources)

            if isinstance(resource, presentation_klass):
                self.assertIn(lun_klass, parent_resources)

        count_before = StorageResourceRecord.objects.count()
        resource_manager.session_remove_resources(controller_record.pk, [presentation_resource])
        count_after = StorageResourceRecord.objects.count()

        self.assertEqual(StorageResourceRecord.objects.filter(resource_class = presentation_klass_id).count(), 0)

        # Check the Lun and DeviceNode are still there but the Presentation is gone
        self.assertEquals(count_after, count_before - 1)

    def test_update_host_lun(self):
        """Test that Volumes are generated from LogicalDrives when they are reported
        in an update rather than the initial resource set"""
        resource_manager = ResourceManager()
        resource_manager.session_open(self.scannable_resource_pk, [self.scannable_resource], 60)
        self.assertEqual(Volume.objects.count(), 0)
        self.assertEqual(VolumeNode.objects.count(), 0)
        resource_manager.session_add_resources(self.scannable_resource_pk, [self.dev_resource, self.node_resource])
        self.assertEqual(Volume.objects.count(), 1)
        self.assertEqual(VolumeNode.objects.count(), 1)
        resource_manager.session_remove_resources(self.scannable_resource_pk, [self.dev_resource, self.node_resource])
        self.assertEqual(Volume.objects.count(), 0)
        self.assertEqual(VolumeNode.objects.count(), 0)

    def test_initial_host_lun(self):
        resource_manager = ResourceManager()
        child_node_resource = self._make_local_resource('linux', 'LinuxDeviceNode',
            path = "/dev/foobar", parents = [self.node_resource], host_id = self.host.id)

        resource_manager.session_open(
            self.scannable_resource_pk, [self.scannable_resource, self.dev_resource, self.node_resource, child_node_resource], 60)

        # Check we got a Volume and a VolumeNode
        self.assertEqual(Volume.objects.count(), 1)
        self.assertEqual(VolumeNode.objects.count(), 1)
        self.assertEqual(Volume.objects.get().label, self.dev_resource.get_label())

        # Check the VolumeNode got the correct path
        self.assertEqual(VolumeNode.objects.get().path, "/dev/foobar")
        self.assertEqual(VolumeNode.objects.get().host, self.host)

        # Check the created Volume has a link back to the UnsharedDevice
        from chroma_core.lib.storage_plugin.query import ResourceQuery
        from chroma_core.models import StorageResourceRecord
        resource_record = StorageResourceRecord.objects.get(pk = self.scannable_resource_pk)
        dev_record = ResourceQuery().get_record_by_attributes('linux', 'ScsiDevice', serial_80 = "foobar", serial_83 = None)

        self.assertEqual(Volume.objects.get().storage_resource_id, dev_record.pk)
        self.assertEqual(Volume.objects.get().size, 4096)

        # Try closing and re-opening the session, this time without the resources, the Volume/VolumeNode objects
        # should be removed
        resource_manager.session_close(resource_record.pk)

        resource_manager.session_open(resource_record.pk, [self.scannable_resource], 60)

        self.assertEqual(VolumeNode.objects.count(), 0)
        self.assertEqual(Volume.objects.count(), 0)

        # TODO: try again, but after creating some targets, check that the Volume/VolumeNode objects are NOT removed

        # TODO: try removing resources in an update_scan and check that Volume/VolumeNode are still removed


class TestEdgeIndex(ResourceManagerTestCase):
    def test_add_remove(self):
        from chroma_core.services.plugin_runner.resource_manager import EdgeIndex

        index = EdgeIndex()
        child = 1
        parent = 2
        index.add_parent(child, parent)
        self.assertEqual(index.get_parents(child), [parent])
        self.assertEqual(index.get_children(parent), [child])
        index.remove_parent(child, parent)
        self.assertEqual(index.get_parents(child), ([]))
        self.assertEqual(index.get_children(parent), ([]))

        index.add_parent(child, parent)
        index.remove_node(parent)
        self.assertEqual(index.get_parents(child), ([]))
        self.assertEqual(index.get_children(parent), ([]))

        index.add_parent(child, parent)
        index.remove_node(child)
        self.assertEqual(index.get_parents(child), ([]))
        self.assertEqual(index.get_children(parent), ([]))

    def test_populate(self):
        from chroma_core.services.plugin_runner.resource_manager import EdgeIndex

        resource_record, couplet_resource = self._make_global_resource('example_plugin', 'Couplet', {'address_1': 'foo', 'address_2': 'bar'})
        controller_resource = self._make_local_resource('example_plugin', 'Controller', index = 0, parents = [couplet_resource])

        resource_manager = ResourceManager()
        resource_manager.session_open(resource_record.pk, [couplet_resource, controller_resource], 60)

        controller_record = StorageResourceRecord.objects.get(~Q(id = resource_record.pk))

        index = EdgeIndex()
        index.populate()
        self.assertEqual(index.get_parents(controller_record.pk), [resource_record.pk])
        self.assertEqual(index.get_children(resource_record.pk), [controller_record.pk])


class TestSubscriberIndex(ResourceManagerTestCase):
    def test_populate(self):
        """Test that the subscriber index is set up correctly for
        resources already in the database"""

        resource_record, scannable_resource = self._make_global_resource('linux', 'PluginAgentResources', {'plugin_name': 'linux', 'host_id': self.host.id})

        scannable_resource_pk = resource_record.pk
        scannable_resource = scannable_resource

        dev_resource = self._make_local_resource('linux', 'UnsharedDevice', path = "/dev/foo", size = 4096)
        node_resource = self._make_local_resource('linux', 'LinuxDeviceNode', path = "/dev/foo", parents = [dev_resource], host_id = self.host.id)

        controller_record, controller_resource = self._make_global_resource('subscription_plugin', 'Controller', {'address': '192.168.0.1'})
        lun_resource = self._make_local_resource('subscription_plugin', 'Lun', lun_id = 'foobar', size = 1024 * 1024)
        presentation_resource = self._make_local_resource('subscription_plugin', 'Presentation', host_id = self.host.id, path = '/dev/foo', lun_id = 'foobar')

        resource_manager = ResourceManager()
        resource_manager.session_open(scannable_resource_pk, [scannable_resource, dev_resource, node_resource], 60)
        resource_manager.session_open(controller_record.pk, [controller_resource, lun_resource, presentation_resource], 60)

        lun_pk = resource_manager._sessions[controller_record.pk].local_id_to_global_id[lun_resource._handle]

        from chroma_core.services.plugin_runner.resource_manager import SubscriberIndex
        index = SubscriberIndex()
        index.populate()

        self.assertEqual(index.what_provides(presentation_resource), set([lun_pk]))


class TestVolumeBalancing(ResourceManagerTestCase):
    def test_volume_balance(self):
        resource_manager = ResourceManager()

        hosts = []
        for i in range(0, 3):
            address = "host_%d" % i
            self.mock_servers[address] = {
                'fqdn': "%s.mycompany.com" % address,
                'nodename': "%s.mycompany.com" % address,
                'nids': ["192.168.0.%d@tcp" % i]
            }

            host, command = ManagedHost.create(self.mock_servers[address]['fqdn'], self.mock_servers[address]['nodename'], ['manage_targets'], address = address)
            resource_record, scannable_resource = self._make_global_resource('linux', 'PluginAgentResources', {'plugin_name': 'linux', 'host_id': host.id})
            hosts.append({'host': host, 'record': resource_record, 'resource': scannable_resource})

            resource_manager.session_open(resource_record.pk, [scannable_resource], 60)

        for vol in range(0, 3):
            devices = ["serial_%s" % i for i in range(0, 3)]
            for host_info in hosts:
                resources = []
                for device in devices:
                    dev_resource = self._make_local_resource('linux', 'ScsiDevice', serial_80 = device, serial_83 = "", size = 4096)
                    node_resource = self._make_local_resource('linux', 'LinuxDeviceNode', path = "/dev/%s" % device, parents = [dev_resource], host_id = host_info['host'].id)
                    resources.extend([dev_resource, node_resource])
                resource_manager.session_add_resources(host_info['record'].pk, resources)

        # Check that for 3 hosts, 3 volumes, they get one primary each
        expected = dict([(host_info['host'].address, 1) for host_info in hosts])
        actual = dict([(host_info['host'].address, VolumeNode.objects.filter(host = host_info['host'], primary = True).count()) for host_info in hosts])
        self.assertDictEqual(expected, actual)


class TestVolumeNaming(ResourceManagerTestCase):
    PLUGIN_LUN_NAME = 'mylun123'
    SERIAL = "123abc456"
    VG = 'foovg'
    LV = 'foolv'

    def _start_plugin_session(self):
        resource_manager = ResourceManager()

        couplet_record, couplet_resource = self._make_global_resource('example_plugin', 'Couplet', {
            'address_1': 'foo',
            'address_2': 'bar'
        })
        lun_resource = self._make_local_resource('example_plugin', 'Lun',
            local_id = 1,
            name = self.PLUGIN_LUN_NAME,
            serial_83 = self.SERIAL.upper(),
            size = 4096
        )

        resource_manager.session_open(couplet_record.pk, [couplet_resource, lun_resource], 60)

    def _start_host_session(self, lvm = False):
        resource_manager = ResourceManager()

        host_record, host_resource = self._make_global_resource('linux', 'PluginAgentResources', {'plugin_name': 'linux', 'host_id': self.host.id})

        dev_resource = self._make_local_resource('linux', 'ScsiDevice', serial_80 = None, serial_83 = self.SERIAL, size = 4096)
        node_resource = self._make_local_resource('linux', 'LinuxDeviceNode', path = "/dev/foo", parents = [dev_resource], host_id = self.host.id)
        resources = [host_resource, dev_resource, node_resource]
        if lvm:
            vg_resource = self._make_local_resource('linux', 'LvmGroup', parents = [node_resource], name = self.VG, uuid = 'b44f7d8e-a40d-4b96-b241-2ab462b4c1c1', size = 4096)
            lv_resource = self._make_local_resource('linux', 'LvmVolume', parents = [vg_resource], name = self.LV, vg = vg_resource, uuid = 'b44f7d8e-a40d-4b96-b241-2ab462b4c1c1', size = 4096)
            lv_node_resource = self._make_local_resource('linux', 'LinuxDeviceNode', parents = [lv_resource], path = "/dev/mapper/%s-%s" % (self.VG, self.LV), host_id = self.host.id)
            resources.extend([vg_resource, lv_resource, lv_node_resource])

        resource_manager.session_open(host_record.pk, resources, 60)

    def assertVolumeName(self, name):
        """For simple single-volume tests, check the name of the volume"""
        self.assertEqual(Volume.objects.count(), 1)
        self.assertEqual(VolumeNode.objects.count(), 1)
        self.assertEqual(Volume.objects.get().label, name)

    def test_name_from_scsi(self):
        """In the absence of a plugin-supplied LUN, name should come from SCSI ID"""
        self._start_host_session()
        self.assertVolumeName(self.SERIAL)

    def test_name_from_plugin_host_first(self):
        """When a plugin supplies a LUN which hooks up via SCSI ID, name should come from the LUN,
        in the harder case where the Volume has been created first and must be updated
        when the plugin resources are added.

        """

        self._start_host_session()
        self._start_plugin_session()
        self.assertVolumeName(self.PLUGIN_LUN_NAME)

    def test_name_from_plugin_host_second(self):
        """When a plugin supplies a LUN which hooks up via SCSI ID, name should come from the LUN,
        in the easier case where the plugin LUN already exists at Volume creation.

        """

        self._start_plugin_session()
        self._start_host_session()
        self.assertVolumeName(self.PLUGIN_LUN_NAME)

    def test_name_from_lvm(self):
        """When a volume corresponds to an LV, the name should come from LVM (not plugin-supplied
        LUN name, even if it's present."""
        self._start_plugin_session()
        self._start_host_session(lvm = True)
        self.assertVolumeName("%s-%s" % (self.VG, self.LV))


class TestAlerts(ResourceManagerTestCase):
    def _update_alerts(self, resource_manager, scannable_pk, resource, alert_klass):
        result = []
        for ac in resource._meta.alert_conditions:
            if isinstance(ac, alert_klass):
                alert_list = ac.test(resource)

                for name, attribute, active in alert_list:
                    resource_manager.session_notify_alert(scannable_pk, resource._handle, active, name, attribute)
                    result.append((name, attribute, active))

        return result

    def _update_alerts_anytrue(self, resource_manager, *args, **kwargs):
        alerts = self._update_alerts(resource_manager, *args, **kwargs)
        for alert in alerts:
            if alert[2]:
                return True
        return False

    def test_multiple_alerts(self):
        """Test multiple AlertConditions acting on the same attribute"""
        resource_record, controller_resource = self._make_global_resource('alert_plugin', 'Controller', {'address': 'foo', 'temperature': 40, 'status': 'OK', 'multi_status': 'OK'})
        lun_resource = self._make_local_resource('alert_plugin', 'Lun', lun_id="foo", size = 1024 * 1024 * 650, parents = [controller_resource])

        # Open session
        resource_manager = ResourceManager()
        resource_manager.session_open(resource_record.pk, [controller_resource, lun_resource], 60)

        from chroma_core.lib.storage_plugin.api.alert_conditions import ValueCondition

        # Go into failed state and send notification
        controller_resource.multi_status = 'FAIL1'
        alerts = self._update_alerts(resource_manager, resource_record.pk, controller_resource, ValueCondition)
        n = 0
        for alert in alerts:
            if alert[2]:
                n += 1

        self.assertEqual(n, 2, alerts)

        # Check that the alert is now set on couplet
        from chroma_core.models import AlertState
        self.assertEqual(AlertState.objects.filter(active = True).count(), 2)

    def test_raise_alert(self):
        resource_record, controller_resource = self._make_global_resource('alert_plugin', 'Controller', {'address': 'foo', 'temperature': 40, 'status': 'OK', 'multi_status': 'OK'})
        lun_resource = self._make_local_resource('alert_plugin', 'Lun', lun_id="foo", size = 1024 * 1024 * 650, parents = [controller_resource])

        # Open session
        resource_manager = ResourceManager()
        resource_manager.session_open(resource_record.pk, [controller_resource, lun_resource], 60)

        from chroma_core.lib.storage_plugin.api.alert_conditions import ValueCondition

        # Go into failed state and send notification
        controller_resource.status = 'FAILED'
        self.assertEqual(True, self._update_alerts_anytrue(resource_manager, resource_record.pk, controller_resource, ValueCondition))

        from chroma_core.models import AlertState, StorageAlertPropagated

        # Check that the alert is now set on couplet
        self.assertEqual(AlertState.objects.filter(active = True).count(), 1)
        # Check that the alert is now set on controller (propagation)
        self.assertEqual(StorageAlertPropagated.objects.filter().count(), 1)

        # Leave failed state and send notification
        controller_resource.status = 'OK'
        self.assertEqual(False, self._update_alerts_anytrue(resource_manager, resource_record.pk, controller_resource, ValueCondition))

        # Check that the alert is now unset on couplet
        self.assertEqual(AlertState.objects.filter(active = True).count(), 0)
        # Check that the alert is now unset on controller (propagation)
        self.assertEqual(StorageAlertPropagated.objects.filter().count(), 0)

    def test_alert_deletion(self):
        resource_record, controller_resource = self._make_global_resource('alert_plugin', 'Controller', {'address': 'foo', 'temperature': 40, 'status': 'OK', 'multi_status': 'OK'})
        lun_resource = self._make_local_resource('alert_plugin', 'Lun', lun_id="foo", size = 1024 * 1024 * 650, parents = [controller_resource])

        # Open session
        resource_manager = ResourceManager()
        resource_manager.session_open(resource_record.pk, [controller_resource, lun_resource], 60)

        from chroma_core.lib.storage_plugin.api.alert_conditions import ValueCondition

        # Go into failed state and send notification
        controller_resource.status = 'FAILED'
        self.assertEqual(True, self._update_alerts_anytrue(resource_manager, resource_record.pk, controller_resource, ValueCondition))

        from chroma_core.models import AlertState, StorageAlertPropagated

        # Check that the alert is now set on couplet
        self.assertEqual(AlertState.objects.filter(active = True).count(), 1)
        # Check that the alert is now set on controller (propagation)
        self.assertEqual(StorageAlertPropagated.objects.filter().count(), 1)

        resource_manager.global_remove_resource(resource_record.pk)

        # Check that the alert is now unset on couplet
        self.assertEqual(AlertState.objects.filter(active = True).count(), 0)
        # Check that the alert is now unset on controller (propagation)
        self.assertEqual(StorageAlertPropagated.objects.filter().count(), 0)

    def test_bound_alert(self):
        resource_record, controller_resource = self._make_global_resource('alert_plugin', 'Controller', {'address': 'foo', 'temperature': 40, 'status': 'OK', 'multi_status': 'OK'})
        lun_resource = self._make_local_resource('alert_plugin', 'Lun', lun_id="foo", size = 1024 * 1024 * 650, parents = [controller_resource])

        from chroma_core.lib.storage_plugin.api.alert_conditions import UpperBoundCondition, LowerBoundCondition

        # Open session
        resource_manager = ResourceManager()
        resource_manager.session_open(resource_record.pk, [controller_resource, lun_resource], 60)

        controller_resource.temperature = 86
        self.assertEqual(True, self._update_alerts_anytrue(resource_manager, resource_record.pk, controller_resource, UpperBoundCondition))

        controller_resource.temperature = 84
        self.assertEqual(False, self._update_alerts_anytrue(resource_manager, resource_record.pk, controller_resource, UpperBoundCondition))

        controller_resource.temperature = -1
        self.assertEqual(True, self._update_alerts_anytrue(resource_manager, resource_record.pk, controller_resource, LowerBoundCondition))

        controller_resource.temperature = 1
        self.assertEqual(False, self._update_alerts_anytrue(resource_manager, resource_record.pk, controller_resource, LowerBoundCondition))
