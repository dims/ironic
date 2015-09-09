# Copyright 2013 Red Hat, Inc.
# All Rights Reserved.
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

import pecan
from pecan import rest
from six.moves import http_client
import wsme
from wsme import types as wtypes

from ironic.api.controllers import base
from ironic.api.controllers import link
from ironic.api.controllers.v1 import types
from ironic.api.controllers.v1 import utils as api_utils
from ironic.api import expose
from ironic.common import exception


# Property information for drivers:
#   key = driver name;
#   value = dictionary of properties of that driver:
#             key = property name.
#             value = description of the property.
# NOTE(rloo). This is cached for the lifetime of the API service. If one or
# more conductor services are restarted with new driver versions, the API
# service should be restarted.
_DRIVER_PROPERTIES = {}

# Vendor information for drivers:
#   key = driver name;
#   value = dictionary of vendor methods of that driver:
#             key = method name.
#             value = dictionary with the metadata of that method.
# NOTE(lucasagomes). This is cached for the lifetime of the API
# service. If one or more conductor services are restarted with new driver
# versions, the API service should be restarted.
_VENDOR_METHODS = {}


class Driver(base.APIBase):
    """API representation of a driver."""

    name = wtypes.text
    """The name of the driver"""

    hosts = [wtypes.text]
    """A list of active conductors that support this driver"""

    links = wsme.wsattr([link.Link], readonly=True)
    """A list containing self and bookmark links"""

    @staticmethod
    def convert_with_links(name, hosts):
        driver = Driver()
        driver.name = name
        driver.hosts = hosts
        driver.links = [
            link.Link.make_link('self',
                                pecan.request.host_url,
                                'drivers', name),
            link.Link.make_link('bookmark',
                                pecan.request.host_url,
                                'drivers', name,
                                bookmark=True)
        ]
        return driver

    @classmethod
    def sample(cls):
        sample = cls(name="sample-driver",
                     hosts=["fake-host"])
        return sample


class DriverList(base.APIBase):
    """API representation of a list of drivers."""

    drivers = [Driver]
    """A list containing drivers objects"""

    @staticmethod
    def convert_with_links(drivers):
        collection = DriverList()
        collection.drivers = [
            Driver.convert_with_links(dname, list(drivers[dname]))
            for dname in drivers]
        return collection

    @classmethod
    def sample(cls):
        sample = cls()
        sample.drivers = [Driver.sample()]
        return sample


class DriverPassthruController(rest.RestController):
    """REST controller for driver passthru.

    This controller allow vendors to expose cross-node functionality in the
    Ironic API. Ironic will merely relay the message from here to the specified
    driver, no introspection will be made in the message body.
    """

    _custom_actions = {
        'methods': ['GET']
    }

    @expose.expose(wtypes.text, wtypes.text)
    def methods(self, driver_name):
        """Retrieve information about vendor methods of the given driver.

        :param driver_name: name of the driver.
        :returns: dictionary with <vendor method name>:<method metadata>
                  entries.
        :raises: DriverNotFound if the driver name is invalid or the
                 driver cannot be loaded.
        """
        if driver_name not in _VENDOR_METHODS:
            topic = pecan.request.rpcapi.get_topic_for_driver(driver_name)
            ret = pecan.request.rpcapi.get_driver_vendor_passthru_methods(
                pecan.request.context, driver_name, topic=topic)
            _VENDOR_METHODS[driver_name] = ret

        return _VENDOR_METHODS[driver_name]

    @expose.expose(wtypes.text, wtypes.text, wtypes.text,
                   body=wtypes.text)
    def _default(self, driver_name, method, data=None):
        """Call a driver API extension.

        :param driver_name: name of the driver to call.
        :param method: name of the method, to be passed to the vendor
                       implementation.
        :param data: body of data to supply to the specified method.
        """
        topic = pecan.request.rpcapi.get_topic_for_driver(driver_name)
        return api_utils.vendor_passthru(driver_name, method, topic, data=data,
                                         driver_passthru=True)


class DriverRaidController(rest.RestController):

    _custom_actions = {
        'logical_disk_properties': ['GET']
    }

    @expose.expose(types.jsontype, wtypes.text)
    def logical_disk_properties(self, driver_name):
        """Returns the logical disk properties for the driver.

        :param driver_name: Name of the driver.
        :returns: A dictionary containing the properties that can be mentioned
            for logical disks and a textual description for them.
        :raises: UnsupportedDriverExtension if the driver doesn't
            support RAID configuration.
        :raises: NotAcceptable, if requested version of the API is less than
            1.12.
        :raises: DriverNotFound, if driver is not loaded on any of the
            conductors.
        """
        if not api_utils.allow_raid_config():
            raise exception.NotAcceptable()
        topic = pecan.request.rpcapi.get_topic_for_driver(driver_name)
        try:
            return pecan.request.rpcapi.get_raid_logical_disk_properties(
                pecan.request.context, driver_name, topic=topic)
        except exception.UnsupportedDriverExtension as e:
            # Change error code as 404 seems appropriate because RAID is a
            # standard interface and all drivers might not have it.
            e.code = http_client.NOT_FOUND
            raise


class DriversController(rest.RestController):
    """REST controller for Drivers."""

    vendor_passthru = DriverPassthruController()

    raid = DriverRaidController()
    """Expose RAID as a sub-element of drivers"""

    _custom_actions = {
        'properties': ['GET'],
    }

    @expose.expose(DriverList)
    def get_all(self):
        """Retrieve a list of drivers."""
        # FIXME(deva): formatting of the auto-generated REST API docs
        #              will break from a single-line doc string.
        #              This is a result of a bug in sphinxcontrib-pecanwsme
        # https://github.com/dreamhost/sphinxcontrib-pecanwsme/issues/8
        driver_list = pecan.request.dbapi.get_active_driver_dict()
        return DriverList.convert_with_links(driver_list)

    @expose.expose(Driver, wtypes.text)
    def get_one(self, driver_name):
        """Retrieve a single driver."""
        # NOTE(russell_h): There is no way to make this more efficient than
        # retrieving a list of drivers using the current sqlalchemy schema, but
        # this path must be exposed for Pecan to route any paths we might
        # choose to expose below it.

        driver_dict = pecan.request.dbapi.get_active_driver_dict()
        for name, hosts in driver_dict.items():
            if name == driver_name:
                return Driver.convert_with_links(name, list(hosts))

        raise exception.DriverNotFound(driver_name=driver_name)

    @expose.expose(wtypes.text, wtypes.text)
    def properties(self, driver_name):
        """Retrieve property information of the given driver.

        :param driver_name: name of the driver.
        :returns: dictionary with <property name>:<property description>
                  entries.
        :raises: DriverNotFound (HTTP 404) if the driver name is invalid or
                 the driver cannot be loaded.
        """
        if driver_name not in _DRIVER_PROPERTIES:
            topic = pecan.request.rpcapi.get_topic_for_driver(driver_name)
            properties = pecan.request.rpcapi.get_driver_properties(
                pecan.request.context, driver_name, topic=topic)
            _DRIVER_PROPERTIES[driver_name] = properties

        return _DRIVER_PROPERTIES[driver_name]
