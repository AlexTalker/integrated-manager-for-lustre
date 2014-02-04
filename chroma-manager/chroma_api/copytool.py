#
# INTEL CONFIDENTIAL
#
# Copyright 2013 Intel Corporation All Rights Reserved.
#
# The source code contained or described herein and all documents related
# to the source code ("Material") are owned by Intel Corporation or its
# suppliers or licensors. Title to the Material remains with Intel Corporation
# or its suppliers and licensors. The Material contains trade secrets and
# proprietary and confidential information of Intel or its suppliers and
# licensors. The Material is protected by worldwide copyright and trade secret
# laws and treaty provisions. No part of the Material may be used, copied,
# reproduced, modified, published, uploaded, posted, transmitted, distributed,
# or disclosed in any way without Intel's prior express written permission.
#
# No license under any patent, copyright, trade secret or other intellectual
# property right is granted to or conferred upon you by disclosure or delivery
# of the Materials, either expressly, by implication, inducement, estoppel or
# otherwise. Any license under such intellectual property rights must be
# express and approved by Intel in writing.


from collections import defaultdict
import copy

from django.core.urlresolvers import resolve
from tastypie import fields
import tastypie.http as http
from tastypie.resources import ModelResource
from tastypie.validation import Validation
from tastypie.authorization import DjangoAuthorization

from chroma_core.models import Copytool, CopytoolOperation, ManagedHost, ManagedFilesystem
from chroma_core.models.copytool import resolve_key
from chroma_api.utils import StatefulModelResource, MetricResource, custom_response
from chroma_api.authentication import AnonymousAuthentication
from chroma_api.host import HostResource
from chroma_api.filesystem import FilesystemResource
from chroma_core.services import log_register
from chroma_core.services.job_scheduler.job_scheduler_client import JobSchedulerClient

log = log_register(__name__)


class CopytoolOperationResource(ModelResource):
    copytool = fields.ToOneField('chroma_api.copytool.CopytoolResource', 'copytool', full = True, null = True)
    description = fields.CharField()
    active_filter = {'started_at__isnull': False, 'finished_at__isnull': True}

    def dehydrate_description(self, bundle):
        return str(bundle.obj)

    def dehydrate_state(self, bundle):
        return resolve_key('state', bundle.obj.state)

    def dehydrate_type(self, bundle):
        return resolve_key('type', bundle.obj.type)

    def build_filters(self, filters=None):
        if filters is None:
            filters = {}

        orm_filters = super(CopytoolOperationResource, self).build_filters(filters)

        if filters.get('active', False):
            orm_filters.update(self.active_filter)

        return orm_filters

    class Meta:
        queryset = CopytoolOperation.objects.select_related().all()
        resource_name = 'copytool_operation'
        excludes = ['not_deleted']
        authorization = DjangoAuthorization()
        authentication = AnonymousAuthentication()
        list_allowed_methods = ['get']
        detail_allowed_methods = ['get']


class CopytoolValidation(Validation):
    def is_valid(self, bundle, request=None):
        errors = defaultdict(list)
        if request.method != 'POST':
            return errors

        for field in ['filesystem', 'host', 'bin_path', 'archive', 'mountpoint']:
            if field not in bundle.data:
                errors[field].append("This field is mandatory")

            if errors:
                return errors

        try:
            HostResource().get_via_uri(bundle.data['host'])
        except ManagedHost.DoesNotExist:
            errors['host'].append("Host not found in DB")

        try:
            FilesystemResource().get_via_uri(bundle.data['filesystem'])
        except ManagedFilesystem.DoesNotExist:
            errors['filesystem'].append("Filesystem not found in DB")

        # TODO: Check copytool path/args against known copytools?

        try:
            int(bundle.data['archive'])
        except ValueError:
            errors['archive'].append("Archive number must be an integer")

        return errors


class CopytoolResource(StatefulModelResource, MetricResource):
    host = fields.ToOneField('chroma_api.host.HostResource', 'host', full = True)
    filesystem = fields.ToOneField('chroma_api.filesystem.FilesystemResource', 'filesystem')
    active_operations_count = fields.IntegerField()

    def hydrate_index(self, bundle):
        if 'index' in bundle.data:
            return bundle

        # This is stupid... Wanted to do it in the model, but the
        # polymorphic stuff messes with the usual save() path.
        existing_filter = dict(
            host = HostResource().get_via_uri(bundle.data['host']),
            filesystem = HostResource().get_via_uri(bundle.data['filesystem']),
            bin_path = bundle.data['bin_path'],
            archive = bundle.data['archive']
        )
        existing = Copytool.objects.filter(**existing_filter).order_by('-index')
        # Increment the index to allow multiple copytool instances on the
        # same worker. Not a typical configuration, but should be allowed.
        if existing:
            bundle.data['index'] = existing[0].index + 1

        return bundle

    def dehydrate_active_operations_count(self, bundle):
        filters = {'copytool__host': bundle.obj.host}
        filters.update(CopytoolOperationResource.active_filter)
        return CopytoolOperation.objects.filter(**filters).count()

    def obj_create(self, bundle, request = None, **kwargs):
        # NB: This is safe because we've already validated the input.
        host_id = resolve(bundle.data['host'])[2]['pk']
        filesystem_id = resolve(bundle.data['filesystem'])[2]['pk']

        # Now take a copy of the data dict and clean it up.
        clean_data = copy.deepcopy(bundle.data)
        clean_data['host'] = host_id
        clean_data['filesystem'] = filesystem_id

        copytool = JobSchedulerClient.create_copytool(clean_data)
        ct_bundle = self.full_dehydrate(self.build_bundle(obj = copytool))
        ct_data = self.alter_detail_data_to_serialize(request, ct_bundle).data

        raise custom_response(self, request, http.HttpAccepted,
                              {'copytool': ct_data})

    class Meta:
        queryset = Copytool.objects.select_related().all()
        resource_name = 'copytool'
        excludes = ['not_deleted']
        authorization = DjangoAuthorization()
        authentication = AnonymousAuthentication()
        validation = CopytoolValidation()
        list_allowed_methods = ['get', 'post']
        detail_allowed_methods = ['get', 'put']
        #readonly = ['host', 'filesystem', 'index', 'path', 'hsm_arguments',
        #            'pid']
        always_return_data = True

        #filtering = {'id': ['exact'],
        #             'filesystem': ['exact'],
        #             'host': ['exact']}