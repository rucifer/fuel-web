# -*- coding: utf-8 -*-

#    Copyright 2013 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Release object and collection
"""
import copy

import six
from sqlalchemy import not_
import yaml

from nailgun import consts
from nailgun.db import db
from nailgun.db.sqlalchemy import models
from nailgun.objects import NailgunCollection
from nailgun.objects import NailgunObject
from nailgun.objects.serializers import release as release_serializer
from nailgun.orchestrator import graph_configuration
from nailgun.settings import settings
from nailgun.utils import extract_env_version


class ReleaseOrchestratorData(NailgunObject):
    """ReleaseOrchestratorData object
    """

    #: SQLAlchemy model
    model = models.ReleaseOrchestratorData

    #: Serializer for ReleaseOrchestratorData
    serializer = release_serializer.ReleaseOrchestratorDataSerializer

    #: JSON schema
    schema = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "title": "ReleaseOrchestratorData",
        "description": "Serialized ReleaseOrchestratorData object",
        "type": "object",
        "required": [
            "release_id"
        ],
        "properties": {
            "id": {"type": "number"},
            "release_id": {"type": "number"},
            "repo_metadata": {"type": "object"},
            "puppet_manifests_source": {"type": "string"},
            "puppet_modules_source": {"type": "string"}
        }
    }

    @classmethod
    def create(cls, data):
        rendered_data = cls.render_data(data)
        return super(ReleaseOrchestratorData, cls).create(rendered_data)

    @classmethod
    def update(cls, instance, data):
        rendered_data = cls.render_data(data)
        return super(ReleaseOrchestratorData, cls).update(
            instance, rendered_data)

    @classmethod
    def render_data(cls, data):
        # Actually, we don't have any reason to make copy at least now.
        # The only reason I want to make copy is to be sure that changed
        # data don't broke something somewhere in the code, since
        # without a copy our changes affect entire application.
        rendered_data = copy.deepcopy(data)

        # create context for rendering
        release = Release.get_by_uid(rendered_data['release_id'])
        context = {
            'MASTER_IP': settings.MASTER_IP,
            'OPENSTACK_VERSION': release.version}

        # render all the paths
        repo_metadata = {}
        for key, value in six.iteritems(rendered_data['repo_metadata']):
            formatted_key = cls.render_path(key, context)
            repo_metadata[formatted_key] = cls.render_path(value, context)
        rendered_data['repo_metadata'] = repo_metadata

        rendered_data['puppet_manifests_source'] = \
            cls.render_path(rendered_data.get(
                'puppet_manifests_source', 'default'), context)

        rendered_data['puppet_modules_source'] = \
            cls.render_path(rendered_data.get(
                'puppet_modules_source', 'default'), context)

        return rendered_data

    @classmethod
    def render_path(cls, path, context):
        return path.format(**context)


class Release(NailgunObject):
    """Release object
    """

    #: SQLAlchemy model for Release
    model = models.Release

    #: Serializer for Release
    serializer = release_serializer.ReleaseSerializer

    #: Release JSON schema
    schema = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "title": "Release",
        "description": "Serialized Release object",
        "type": "object",
        "required": [
            "name",
            "operating_system"
        ],
        "properties": {
            "id": {"type": "number"},
            "name": {"type": "string"},
            "version": {"type": "string"},
            "can_update_from_versions": {"type": "array"},
            "description": {"type": "string"},
            "operating_system": {"type": "string"},
            "state": {
                "type": "string",
                "enum": list(consts.RELEASE_STATES)
            },
            "networks_metadata": {"type": "array"},
            "attributes_metadata": {"type": "object"},
            "volumes_metadata": {"type": "object"},
            "modes_metadata": {"type": "object"},
            "roles_metadata": {"type": "object"},
            "wizard_metadata": {"type": "object"},
            "roles": {"type": "array"},
            "clusters": {"type": "array"},
            "is_deployable": {"type": "boolean"}
        }
    }

    @classmethod
    def create(cls, data):
        """Create Release instance with specified parameters in DB.
        Corresponding roles are created in DB using names specified
        in "roles" field. See :func:`update_roles`

        :param data: dictionary of key-value pairs as object fields
        :returns: Release instance
        """
        roles = data.pop("roles", None)
        orch_data = data.pop("orchestrator_data", None)
        new_obj = super(Release, cls).create(data)
        if roles:
            cls.update_roles(new_obj, roles)
        if orch_data:
            orch_data["release_id"] = new_obj.id
            ReleaseOrchestratorData.create(orch_data)
        return new_obj

    @classmethod
    def update(cls, instance, data):
        """Update existing Release instance with specified parameters.
        Corresponding roles are updated in DB using names specified
        in "roles" field. See :func:`update_roles`

        :param instance: Release instance
        :param data: dictionary of key-value pairs as object fields
        :returns: Release instance
        """
        roles = data.pop("roles", None)
        orch_data = data.pop("orchestrator_data", None)
        super(Release, cls).update(instance, data)
        if roles is not None:
            cls.update_roles(instance, roles)
        if orch_data:
            cls.update_orchestrator_data(instance, orch_data)
        return instance

    @classmethod
    def update_roles(cls, instance, roles):
        """Update existing Release instance with specified roles.
        Previous ones are deleted.

        IMPORTANT NOTE: attempting to remove roles that are already
        assigned to nodes will lead to an Exception.

        :param instance: Release instance
        :param roles: list of new roles names
        :returns: None
        """
        db().query(models.Role).filter(
            not_(models.Role.name.in_(roles))
        ).filter(
            models.Role.release_id == instance.id
        ).delete(synchronize_session='fetch')
        db().refresh(instance)

        added_roles = instance.roles
        for role in roles:
            if role not in added_roles:
                new_role = models.Role(
                    name=role,
                    release=instance
                )
                db().add(new_role)
                added_roles.append(role)
        db().flush()

    @classmethod
    def update_orchestrator_data(cls, instance, orchestrator_data):
        orchestrator_data.pop("id", None)
        orchestrator_data["release_id"] = instance.id

        ReleaseOrchestratorData.update(
            instance.orchestrator_data, orchestrator_data)

    @classmethod
    def get_orchestrator_data_dict(cls, instance):
        data = instance.orchestrator_data
        return ReleaseOrchestratorData.serializer.serialize(data)

    @classmethod
    def is_deployable(cls, instance):
        """Returns whether a given release deployable or not.

        :param instance: a Release instance
        :returns: True if a given release is deployable; otherwise - False
        """
        # in experimental mode we deploy all releases
        if 'experimental' in settings.VERSION['feature_groups']:
            return True
        return instance.is_deployable

    @classmethod
    def get_deployment_tasks(cls, instance):
        """Get deployment graph based on release version."""
        env_version = extract_env_version(instance.version)
        if instance.deployment_tasks:
            return instance.deployment_tasks
        elif env_version.startswith('5.0'):
            return yaml.load(graph_configuration.DEPLOYMENT_50)
        else:
            return yaml.load(graph_configuration.DEPLOYMENT_CURRENT)


class ReleaseCollection(NailgunCollection):
    """Release collection
    """

    #: Single Release object class
    single = Release
