#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2020      David Straub
# Copyright (C) 2020      Christopher Horn
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""Person API resource."""

from typing import Dict

from flask import g
from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.lib import Person
from gramps.gen.utils.grampslocale import GrampsLocale

from .base import (
    GrampsObjectProtectedResource,
    GrampsObjectResourceHelper,
    GrampsObjectsProtectedResource,
)
from .util import (
    get_extended_attributes,
    get_family_by_handle,
    get_person_profile_for_object,
)



class PersonResourceHelper(GrampsObjectResourceHelper):
    """Person resource helper."""

    gramps_class_name = "Person"

    def object_extend(
        self, obj: Person, args: Dict, locale: GrampsLocale = glocale
    ) -> Person:
        """Extend person attributes as needed."""
        db_handle = self.db_handle
        if "profile" in args:
            obj.profile = get_person_profile_for_object(
                db_handle,
                obj,
                args["profile"],
                locale=locale,
                name_format=args.get("name_format"),
                precision=args.get("precision", 3),
            )
        if "extend" in args:
            obj.extended = get_extended_attributes(db_handle, obj, args)
            family_cache = getattr(g, "_family_extend_cache", None)
            if "all" in args["extend"] or "family_list" in args["extend"]:
                if family_cache is not None:
                    obj.extended["families"] = [
                        family_cache[handle]
                        for handle in obj.family_list
                        if handle in family_cache
                    ]
                else:
                    obj.extended["families"] = [
                        get_family_by_handle(db_handle, handle)
                        for handle in obj.family_list
                    ]
            if "all" in args["extend"] or "parent_family_list" in args["extend"]:
                if family_cache is not None:
                    obj.extended["parent_families"] = [
                        family_cache[handle]
                        for handle in obj.parent_family_list
                        if handle in family_cache
                    ]
                else:
                    obj.extended["parent_families"] = [
                        get_family_by_handle(db_handle, handle)
                        for handle in obj.parent_family_list
                    ]
            if "all" in args["extend"] or "primary_parent_family" in args["extend"]:
                main_handle = obj.get_main_parents_family_handle()
                if family_cache is not None and main_handle in family_cache:
                    obj.extended["primary_parent_family"] = family_cache[main_handle]
                else:
                    obj.extended["primary_parent_family"] = get_family_by_handle(
                        db_handle, main_handle
                    )
        return obj


class PersonResource(GrampsObjectProtectedResource, PersonResourceHelper):
    """Person resource."""


class PeopleResource(GrampsObjectsProtectedResource, PersonResourceHelper):
    """People resource."""
