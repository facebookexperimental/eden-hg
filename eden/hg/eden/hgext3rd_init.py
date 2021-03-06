# Copyright (c) 2017-present, Facebook, Inc.
# All Rights Reserved.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

from __future__ import absolute_import, division, print_function, unicode_literals

import pkgutil


# Indicate that hgext3rd is a namspace package, and other python path
# directories may still be searched for hgext3rd extensions.
__path__ = pkgutil.extend_path(__path__, __name__)  # type: ignore # noqa: F821
