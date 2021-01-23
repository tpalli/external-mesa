# coding=utf-8
#
# Copyright © 2015, 2017 Intel Corporation
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice (including the next
# paragraph) shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.
#

import argparse
import math
import os
import xml.etree.ElementTree as et

from collections import OrderedDict, namedtuple
from mako.template import Template

from anv_extensions import VkVersion, MAX_API_VERSION, EXTENSIONS

# We generate a static hash table for entry point lookup
# (vkGetProcAddress). We use a linear congruential generator for our hash
# function and a power-of-two size table. The prime numbers are determined
# experimentally.

LAYERS = [
    'anv',
    'gen7',
    'gen75',
    'gen8',
    'gen9',
    'gen11',
    'gen12',
    'gen125',
]

TEMPLATE_H = Template("""\
/* This file generated from ${filename}, don't edit directly. */

#include "vk_entrypoints.h"

extern const struct vk_instance_dispatch_table anv_instance_dispatch_table;
extern const struct vk_physical_device_dispatch_table anv_physical_device_dispatch_table;
%for layer in LAYERS:
extern const struct vk_device_dispatch_table ${layer}_device_dispatch_table;
%endfor

% for e in instance_entrypoints:
  % if e.alias and e.alias.enabled:
    <% continue %>
  % endif
  % if e.guard is not None:
#ifdef ${e.guard}
  % endif
  ${e.return_type} ${e.prefixed_name('anv')}(${e.decl_params()});
  % if e.guard is not None:
#endif // ${e.guard}
  % endif
% endfor

% for e in physical_device_entrypoints:
  % if e.alias:
    <% continue %>
  % endif
  % if e.guard is not None:
#ifdef ${e.guard}
  % endif
  ${e.return_type} ${e.prefixed_name('anv')}(${e.decl_params()});
  % if e.guard is not None:
#endif // ${e.guard}
  % endif
% endfor

% for e in device_entrypoints:
  % if e.alias and e.alias.enabled:
    <% continue %>
  % endif
  % if e.guard is not None:
#ifdef ${e.guard}
  % endif
  % for layer in LAYERS:
  ${e.return_type} ${e.prefixed_name(layer)}(${e.decl_params()});
  % endfor
  % if e.guard is not None:
#endif // ${e.guard}
  % endif
% endfor
""", output_encoding='utf-8')

TEMPLATE_C = Template(u"""\
/*
 * Copyright © 2015 Intel Corporation
 *
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice (including the next
 * paragraph) shall be included in all copies or substantial portions of the
 * Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
 * FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
 * IN THE SOFTWARE.
 */

/* This file generated from ${filename}, don't edit directly. */

#include "anv_private.h"

/* Weak aliases for all potential implementations. These will resolve to
 * NULL if they're not defined, which lets the resolve_entrypoint() function
 * either pick the correct entry point.
 */

% for e in instance_entrypoints:
  % if e.alias and e.alias.enabled:
    <% continue %>
  % endif
  % if e.guard is not None:
#ifdef ${e.guard}
  % endif
  ${e.return_type} ${e.prefixed_name('anv')}(${e.decl_params()}) __attribute__ ((weak));
  % if e.guard is not None:
#endif // ${e.guard}
  % endif
% endfor

const struct vk_instance_dispatch_table anv_instance_dispatch_table = {
% for e in instance_entrypoints:
  % if e.guard is not None:
#ifdef ${e.guard}
  % endif
  .${e.name} = ${e.prefixed_name('anv')},
  % if e.guard is not None:
#endif // ${e.guard}
  % endif
% endfor
};

% for e in physical_device_entrypoints:
  % if e.alias and e.alias.enabled:
    <% continue %>
  % endif
  % if e.guard is not None:
#ifdef ${e.guard}
  % endif
  ${e.return_type} ${e.prefixed_name('anv')}(${e.decl_params()}) __attribute__ ((weak));
  % if e.guard is not None:
#endif // ${e.guard}
  % endif
% endfor

const struct vk_physical_device_dispatch_table anv_physical_device_dispatch_table = {
% for e in physical_device_entrypoints:
  % if e.guard is not None:
#ifdef ${e.guard}
  % endif
  .${e.name} = ${e.prefixed_name('anv')},
  % if e.guard is not None:
#endif // ${e.guard}
  % endif
% endfor
};


% for layer in LAYERS:
  % for e in device_entrypoints:
    % if e.alias and e.alias.enabled:
      <% continue %>
    % endif
    % if e.guard is not None:
#ifdef ${e.guard}
    % endif
    ${e.return_type} ${e.prefixed_name(layer)}(${e.decl_params()}) __attribute__ ((weak));
    % if e.guard is not None:
#endif // ${e.guard}
    % endif
  % endfor

  const struct vk_device_dispatch_table ${layer}_device_dispatch_table = {
  % for e in device_entrypoints:
    % if e.guard is not None:
#ifdef ${e.guard}
    % endif
    .${e.name} = ${e.prefixed_name(layer)},
    % if e.guard is not None:
#endif // ${e.guard}
    % endif
  % endfor
  };
% endfor
""", output_encoding='utf-8')

EntrypointParam = namedtuple('EntrypointParam', 'type name decl')

class EntrypointBase(object):
    def __init__(self, name):
        assert name.startswith('vk')
        self.name = name[2:]
        self.alias = None
        self.guard = None
        self.enabled = False
        self.num = None
        # Extensions which require this entrypoint
        self.core_version = None
        self.extensions = []

    def prefixed_name(self, prefix):
        return prefix + '_' + self.name

class Entrypoint(EntrypointBase):
    def __init__(self, name, return_type, params, guard=None):
        super(Entrypoint, self).__init__(name)
        self.return_type = return_type
        self.params = params
        self.guard = guard

    def is_physical_device_entrypoint(self):
        return self.params[0].type in ('VkPhysicalDevice', )

    def is_device_entrypoint(self):
        return self.params[0].type in ('VkDevice', 'VkCommandBuffer', 'VkQueue')

    def decl_params(self):
        return ', '.join(p.decl for p in self.params)

    def call_params(self):
        return ', '.join(p.name for p in self.params)

class EntrypointAlias(EntrypointBase):
    def __init__(self, name, entrypoint):
        super(EntrypointAlias, self).__init__(name)
        self.alias = entrypoint

    def is_physical_device_entrypoint(self):
        return self.alias.is_physical_device_entrypoint()

    def is_device_entrypoint(self):
        return self.alias.is_device_entrypoint()

    def prefixed_name(self, prefix):
        if self.alias.enabled:
            return self.alias.prefixed_name(prefix)
        return super(EntrypointAlias, self).prefixed_name(prefix)

    @property
    def params(self):
        return self.alias.params

    @property
    def return_type(self):
        return self.alias.return_type

    def decl_params(self):
        return self.alias.decl_params()

    def call_params(self):
        return self.alias.call_params()

def get_entrypoints(doc, entrypoints_to_defines):
    """Extract the entry points from the registry."""
    entrypoints = OrderedDict()

    for command in doc.findall('./commands/command'):
        if 'alias' in command.attrib:
            alias = command.attrib['name']
            target = command.attrib['alias']
            entrypoints[alias] = EntrypointAlias(alias, entrypoints[target])
        else:
            name = command.find('./proto/name').text
            ret_type = command.find('./proto/type').text
            params = [EntrypointParam(
                type=p.find('./type').text,
                name=p.find('./name').text,
                decl=''.join(p.itertext())
            ) for p in command.findall('./param')]
            guard = entrypoints_to_defines.get(name)
            # They really need to be unique
            assert name not in entrypoints
            entrypoints[name] = Entrypoint(name, ret_type, params, guard)

    for feature in doc.findall('./feature'):
        assert feature.attrib['api'] == 'vulkan'
        version = VkVersion(feature.attrib['number'])
        if version > MAX_API_VERSION:
            continue

        for command in feature.findall('./require/command'):
            e = entrypoints[command.attrib['name']]
            e.enabled = True
            assert e.core_version is None
            e.core_version = version

    supported_exts = dict((ext.name, ext) for ext in EXTENSIONS)
    for extension in doc.findall('.extensions/extension'):
        ext_name = extension.attrib['name']
        if ext_name not in supported_exts:
            continue

        ext = supported_exts[ext_name]
        ext.type = extension.attrib['type']

        for command in extension.findall('./require/command'):
            e = entrypoints[command.attrib['name']]
            e.enabled = True
            assert e.core_version is None
            e.extensions.append(ext)

    return [e for e in entrypoints.values() if e.enabled]


def get_entrypoints_defines(doc):
    """Maps entry points to extension defines."""
    entrypoints_to_defines = {}

    platform_define = {}
    for platform in doc.findall('./platforms/platform'):
        name = platform.attrib['name']
        define = platform.attrib['protect']
        platform_define[name] = define

    for extension in doc.findall('./extensions/extension[@platform]'):
        platform = extension.attrib['platform']
        define = platform_define[platform]

        for entrypoint in extension.findall('./require/command'):
            fullname = entrypoint.attrib['name']
            entrypoints_to_defines[fullname] = define

    return entrypoints_to_defines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--outdir', help='Where to write the files.',
                        required=True)
    parser.add_argument('--xml',
                        help='Vulkan API XML file.',
                        required=True,
                        action='append',
                        dest='xml_files')
    args = parser.parse_args()

    entrypoints = []

    for filename in args.xml_files:
        doc = et.parse(filename)
        entrypoints += get_entrypoints(doc, get_entrypoints_defines(doc))

    # Manually add CreateDmaBufImageINTEL for which we don't have an extension
    # defined.
    entrypoints.append(Entrypoint('vkCreateDmaBufImageINTEL', 'VkResult', [
        EntrypointParam('VkDevice', 'device', 'VkDevice device'),
        EntrypointParam('VkDmaBufImageCreateInfo', 'pCreateInfo',
                        'const VkDmaBufImageCreateInfo* pCreateInfo'),
        EntrypointParam('VkAllocationCallbacks', 'pAllocator',
                        'const VkAllocationCallbacks* pAllocator'),
        EntrypointParam('VkDeviceMemory', 'pMem', 'VkDeviceMemory* pMem'),
        EntrypointParam('VkImage', 'pImage', 'VkImage* pImage')
    ]))

    device_entrypoints = []
    physical_device_entrypoints = []
    instance_entrypoints = []
    for e in entrypoints:
        if e.is_device_entrypoint():
            device_entrypoints.append(e)
        elif e.is_physical_device_entrypoint():
            physical_device_entrypoints.append(e)
        else:
            instance_entrypoints.append(e)

    # For outputting entrypoints.h we generate a anv_EntryPoint() prototype
    # per entry point.
    try:
        with open(os.path.join(args.outdir, 'anv_entrypoints.h'), 'wb') as f:
            f.write(TEMPLATE_H.render(instance_entrypoints=instance_entrypoints,
                                      physical_device_entrypoints=physical_device_entrypoints,
                                      device_entrypoints=device_entrypoints,
                                      LAYERS=LAYERS,
                                      filename=os.path.basename(__file__)))
        with open(os.path.join(args.outdir, 'anv_entrypoints.c'), 'wb') as f:
            f.write(TEMPLATE_C.render(instance_entrypoints=instance_entrypoints,
                                      physical_device_entrypoints=physical_device_entrypoints,
                                      device_entrypoints=device_entrypoints,
                                      LAYERS=LAYERS,
                                      filename=os.path.basename(__file__)))
    except Exception:
        # In the event there's an error, this imports some helpers from mako
        # to print a useful stack trace and prints it, then exits with
        # status 1, if python is run with debug; otherwise it just raises
        # the exception
        if __debug__:
            import sys
            from mako import exceptions
            sys.stderr.write(exceptions.text_error_template().render() + '\n')
            sys.exit(1)
        raise


if __name__ == '__main__':
    main()
