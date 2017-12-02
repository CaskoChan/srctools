"""Parse FGD files, used to describe Hammer entities."""
from enum import Enum
from struct import Struct
import re
import io
import math

from typing import Dict, Iterator, Union, T, Mapping, Tuple, List

from srctools.filesys import FileSystem, File
from srctools.tokenizer import Tokenizer, Token, TokenSyntaxError

__all__ = [
    'ValueTypes', 'EntityTypes'
    'KeyValError', 'FGD', 'EntityDef',
]

_fmt_8bit = Struct('>B')
_fmt_16bit = Struct('>H')
_fmt_32bit = Struct('>I')
_fmt_double = Struct('>d')
_fmt_header = Struct('>BddI')
_fmt_ent_header = Struct('<BBBBB')

def _read_struct(format: Struct, file):
    return format.unpack(file.read(format.size))

# Version number for the format.
BIN_FORMAT_VERSION = 1

# "text" with an optional '+'
_RE_DOC_LINE = re.compile(r'\s*"([^"]*)"\s*(\+)?\s*')

_RE_KEYVAL_LINE = re.compile(
    r''' (input | output)? \s* # Input or output name
    (\w+)\s*\(\s*(\w+)\s*\) # Name, (type)
    \s* (report | readonly)?  # Flags for the text
    (?: \s* : \s* \"([^"]*)\"\s* # Display name
        (\+)? \s* # IO only - plus for continued description
        (?::([^:]+)  # Default
            (?::([^:]+)  # Docs
            )?
        )?
    )? # Optional for spawnflags..
    \s* (=)? # Has equal sign?
    ''',
    re.VERBOSE,
)

_RE_HELPERS = re.compile(
    r'''(\w+)\s* \( \s* ([^)]*) \s* \)''',
    re.VERBOSE,
)
_RE_HELPER_ARGS = re.compile(r'\s*\,\s*')

class FGDParseError(TokenSyntaxError):
    pass

class ValueTypes(Enum):
    """Types which can be applied to a KeyValue."""
    # Special cases:
    VOID = 'void'  # Nothing
    CHOICES = 'choices'  # Special - preset value list as string
    SPAWNFLAGS = 'flags'  # Binary flag values.

    # Simple values
    STRING = 'string'
    BOOL = 'boolean'
    INT = 'integer'
    FLOAT = 'float'
    VEC = 'vector'  # Offset or the like
    ANGLES = 'angle'  # Rotation

    # String targetname values (need fixups)
    TARG_DEST = 'target_destination'  # A targetname of another ent.
    TARG_DEST_CLASS = 'target_name_or_class'  # Above + classnames.
    TARG_SOURCE = 'target_source'  # The 'targetname' keyvalue.
    TARG_NPC_CLASS = 'npcclass'  # targetnames filtered to NPC ents
    TARG_POINT_CLASS = 'pointentityclass'  # targetnames filtered to point entities.
    TARG_FILTER_NAME = 'filterclass'  # targetnames of filters.
    TARG_NODE_DEST = 'node_dest'  # name of a node
    TARG_NODE_SOURCE = 'node_id'  # name of us

    # Strings, don't need fixups
    STR_SCENE = 'scene'  # VCD files
    STR_SOUND = 'sound'  # WAV & SoundScript
    STR_PARTICLE = 'particlesystem'  # Particles
    STR_SPRITE = 'sprite'  # Sprite materials
    STR_DECAL = 'decal'  # Sprite materials
    STR_MATERIAL = 'material'  # Materials
    STR_MODEL = 'studio'  # Model
    STR_VSCRIPT = 'scriptlist'  # List of vscripts

    # More complex
    ANGLE_NEG_PITCH = 'angle_negative_pitch'  # Inverse pitch of 'angles'
    VEC_LINE = 'vecline'  # Absolute vector, with line drawn from origin to point
    VEC_ORIGIN = 'origin'  # Used for 'origin' keyvalue
    VEC_AXIS = 'axis'
    COLOR_1 = 'color1'  # RGB 0-1 + extra
    COLOR_255 = 'color255'  # RGB 0-255 + extra
    SIDE_LIST = 'sidelist'  # Space-seperated list of sides.

    # Instances
    INST_FILE = 'instance_file'  # File of func_instance
    INST_VAR_DEF = 'instance_parm'  # $fixup definition
    INST_VAR_REP = 'instance_variable'  # $fixup usage

    @property
    def has_list(self):
        """Is this a flag or choices value, and needs a [] list?"""
        return self.value in ('choices', 'flags')

VALUE_TYPE_LOOKUP = {
    typ.value: typ
    for typ in ValueTypes
}
# These have two names pointing to the same type...
VALUE_TYPE_LOOKUP['bool'] = ValueTypes.BOOL
VALUE_TYPE_LOOKUP['int'] = ValueTypes.INT


class EntityTypes(Enum):
    BASE = 'baseclass'  # Not an entity, others inherit from this.
    POINT = 'pointclass'  # Point entity
    BRUSH = 'solidclass'  # Brush entity. Can't have 'model'
    ROPES = 'keyframeclass'  # Used for move_rope etc
    TRACK = 'moveclass'  # Used for path_track etc
    FILTER = 'filterclass'  # Used for filters
    NPC = 'npcclass'  # An NPC


class HelperTypes(Enum):
    """Types of functions in the entity header."""
    INHERIT = 'base'

    # Snap to 1/2 of grid.
    # Special - no arguments.
    HALF_GRID_SNAP = 'halfgridsnap'

    # Simple helpers
    CUBE = 'size'  # Sets size of purple cube
    BBOX = 'bbox'  # Sets bounding box of entity
    TINT = 'color'
    SPHERE = 'sphere'
    LINE = 'line'
    FRUSTUM = 'frustum'
    CYLINDER = 'cylinder'
    BRUSH_SIDES = 'sidelist'
    BOUNDING_BOX_HELPER = 'wirebox'  # Displays bounding box from two keyvalues

    # Complex helpers using resources
    SPRITE = 'iconsprite'
    MODEL = 'studio'
    MODEL_PROP = 'studioprop'
    MODEL_NEG_PITCH = 'lightprop'  # Uses separate pitch keyvalue

    # Specialty for certain ents
    ENT_SPRITE = 'sprite'
    ENT_INSTANCE = 'instance'
    ENT_DECAL = 'decal'
    ENT_OVERLAY = 'overlay'
    ENT_OVERLAY_WATER = 'overlay_transition'
    ENT_LIGHT = 'light'
    ENT_LIGHT_CONE = 'lightcone'
    ENT_ROPE = 'keyframe'
    ENT_TRACK = 'animator'
    ENT_BREAKABLE_SURF = 'quadbounds'  # Sets the 4 corners on save
    
# Ordered list of value types, for encoding in the binary
# format. All must be here, new ones should be added at the end.
VALUE_TYPE_ORDER = [
    ValueTypes.VOID,
    ValueTypes.CHOICES,
    ValueTypes.SPAWNFLAGS,

    ValueTypes.STRING,
    ValueTypes.BOOL,
    ValueTypes.INT,
    ValueTypes.FLOAT,
    ValueTypes.VEC,
    ValueTypes.ANGLES,

    ValueTypes.TARG_DEST,
    ValueTypes.TARG_DEST_CLASS,
    ValueTypes.TARG_SOURCE,
    ValueTypes.TARG_NPC_CLASS,
    ValueTypes.TARG_POINT_CLASS,
    ValueTypes.TARG_FILTER_NAME,
    ValueTypes.TARG_NODE_DEST,
    ValueTypes.TARG_NODE_SOURCE,

    # Strings, don't need fixups
    ValueTypes.STR_SCENE,
    ValueTypes.STR_SOUND,
    ValueTypes.STR_PARTICLE,
    ValueTypes.STR_SPRITE,
    ValueTypes.STR_DECAL,
    ValueTypes.STR_MATERIAL,
    ValueTypes.STR_MODEL,
    ValueTypes.STR_VSCRIPT,

    ValueTypes.ANGLE_NEG_PITCH,
    ValueTypes.VEC_LINE,
    ValueTypes.VEC_ORIGIN,
    ValueTypes.VEC_AXIS,
    ValueTypes.COLOR_1,
    ValueTypes.COLOR_255,
    ValueTypes.SIDE_LIST,

    ValueTypes.INST_FILE,
    ValueTypes.INST_VAR_DEF,
    ValueTypes.INST_VAR_REP,
]

# Ditto for entity types.
ENTITY_TYPE_ORDER = [
    EntityTypes.BASE,
    EntityTypes.POINT,
    EntityTypes.BRUSH,
    EntityTypes.ROPES,
    EntityTypes.TRACK,
    EntityTypes.FILTER,
    EntityTypes.NPC,
]

assert set(VALUE_TYPE_ORDER) == set(ValueTypes), \
    "Missing values: " +repr(set(ValueTypes) - set(VALUE_TYPE_ORDER))
assert set(ENTITY_TYPE_ORDER) == set(EntityTypes), \
    "Missing values: " +repr(set(EntityTypes) - set(ENTITY_TYPE_ORDER))
    
# Can only store this many in the bytes.
assert len(VALUE_TYPE_ORDER) < 127, "Too many values."
assert len(ENTITY_TYPE_ORDER) < 255, "Too many entity types."
    
VALUE_TYPE_INDEX = {val: ind for (ind, val) in enumerate(VALUE_TYPE_ORDER)}
ENTITY_TYPE_INDEX = {ent: ind for (ind, ent) in enumerate(ENTITY_TYPE_ORDER)}


def read_colon_list(tok: Tokenizer, had_colon=False):
    """Read strings seperated by colons, up to the end of the line.
    
    The token found at the end is returned.
    """
    strings = []
    ready_for_string = had_colon  # Did we have a colon before?
    token = Token.EOF
    for token, tok_value in tok:
        if token is Token.STRING:
            if not ready_for_string:
                raise tok.error('Too many strings ({!r})!', tok_value)
            strings.append(tok_value)
            ready_for_string = False
        elif token is Token.COLON:
            if ready_for_string:
                # ': :' means to have an empty string there.
                strings.append('')
            ready_for_string = True
        elif token is Token.PLUS:
            if ready_for_string or not strings:
                raise tok.error('"+" without a string before it!')
            strings[-1] += tok.expect(Token.STRING)
        elif ready_for_string and token is Token.NEWLINE:
            continue # skip over this in particular..
        else:
            if ready_for_string:
                raise tok.error(token)
            return strings, token
    else:
        raise tok.error(token)
        
class BinStrDict:
    """Manages a "dictionary" for compressing repeated strings in the binary format.
    
    Each unique string is assigned a 2-byte index into the list.
    """
    
    def __init__(self):
        self._dict = {}
        self.cur_index = 0
        
    def __call__(self, string: str) -> bytes:
        """Get the index for a string. 
        
        If not already present it is assigned one.
        The result is the two bytes that represent the string.
        """
        try:
            index = self._dict[string]
        except KeyError:
            index = self._dict[string] = self.cur_index
            self.cur_index += 1
            # Check it can actually fit.
            if index > (1 << 16):
                raise ValueError("Too many items in dictionary!")
                
        return _fmt_16bit.pack(index)
        
    def serialise(self, file):
        """Convert this to a stream of bytes."""
        inv_list = [''] * len(self._dict)
        for txt, ind in self._dict.items():
            inv_list[ind] = txt
        
        file.write(_fmt_32bit.pack(len(inv_list)))
        for txt in inv_list:
            file.write(_fmt_16bit.pack(len(txt)))
            file.write(txt.encode('utf8'))
     
    @staticmethod       
    def unserialise(file):
        """Read the dictionary from a file.
        
        This returns a function which reads
        a string from a file at the current point. 
        """
        [length] = _read_struct(_fmt_32bit, file)
        inv_list = [''] * length
        for ind in range(length):
            [str_len] = _read_struct(_fmt_16bit, file)
            inv_list[ind] = file.read(str_len).decode('utf8')

        def lookup(file) -> str:
            """Read the index from the file, and return the string it matches."""
            [index] = _read_struct(_fmt_16bit, file)
            return inv_list[index]
        
        return lookup


class KeyValues:
    """Represents a generic keyvalue type."""
    def __init__(
        self,
        name: str,
        val_type: ValueTypes,
        disp_name: str,
        default: str,
        doc: str,
        val_list: List[Union[Tuple[int, str, bool], Tuple[str, str]]],
        is_readonly: bool,
    ):
        self.name = name
        self.type = val_type
        self.default = default
        self.disp_name = disp_name
        self.desc = doc
        self.val_list = val_list
        self.readonly = is_readonly
        
    def __repr__(self):
        return 'KeyValues({s.name!r}, {s.type!r}, {s.disp_name!r}, {s.default!r}, {s.desc!r}, {s.val_list!r}, {s.readonly})'.format(s=self)
        
    def serialise(self, file, str_dict: BinStrDict):
        """Write to the binary file."""
        file.write(str_dict(self.name))
        file.write(str_dict(self.disp_name))
        value_type = VALUE_TYPE_INDEX[self.type]
        # Use the high bit to store this inside here as well.
        if self.readonly:
            value_type |= 128
        file.write(_fmt_8bit.pack(value_type))
        
        # Spawnflags have integer names and defaults,
        # choices has string values and no default.
        if self.type is ValueTypes.SPAWNFLAGS:
            file.write(_fmt_8bit.pack(len(self.val_list)))
            # spawnflags go up to at least 1<<23.
            for val, name, default in self.val_list:
                # We can write 2^n instead of the full number,
                # since they're all powers of two.
                power = int(math.log2(val))
                if default: # Pack the default as the MSB.
                    power |= 128
                file.write(_fmt_8bit.pack(power))
                file.write(str_dict(name))
            return # Spawnflags doesn't need to write a default.
        
        file.write(str_dict(self.default or ''))
        
        if self.type is ValueTypes.CHOICES:
            # Use two bytes, these can be large (soundscapes).
            file.write(_fmt_16bit.pack(len(self.val_list)))
            for val, name in self.val_list:
                file.write(str_dict(val))
                file.write(str_dict(name))
        
    @staticmethod
    def unserialise(file, from_dict):
        name = from_dict(file)
        disp_name = from_dict(file)
        [value_ind] = _read_struct(_fmt_8bit, file)
        readonly = value_ind & 128
        value_type = VALUE_TYPE_ORDER[value_ind & 127]
        
        val_list = None
        
        if value_type is ValueTypes.SPAWNFLAGS:
            default = '' # No default for this type.
            [val_count] = _read_struct(_fmt_8bit, file)
            val_list = [0] * val_count
            for ind in range(val_count):
                [power] = _read_struct(_fmt_8bit, file)
                val_name = from_dict(file)
                val_list[ind] = (1<<(power & 127), val_name, (power & 128) != 0)
        else:
            default = from_dict(file)
            
            if value_type is ValueTypes.CHOICES:
                [val_count] = _read_struct(_fmt_16bit, file)
                val_list = [0] * val_count
                for ind in range(val_count):
                    val_list[ind] = (from_dict(file), from_dict(file))
        
        
        return KeyValues(
            name,
            value_type,
            disp_name,
            default,
            '',
            val_list,
            readonly,
        )


class IODef:
    """Represents an input or output for an entity."""
    def __init__(self, name, val_type: ValueTypes, description: str=''):
        self.name = name
        self.type = val_type
        self.desc = description
        
    def __repr__(self):
        txt = '{}({!r}, {!r}'.format(
            self.__class__.__name__,
            self.name,
            self.type,
        )
        if self.desc:
            txt += ', ' + repr(self.desc)
        return txt + ')'
        
    def serialise(self, file, dic: BinStrDict):
        file.write(dic(self.name))
        file.write(_fmt_8bit.pack(VALUE_TYPE_INDEX[self.type]))
        
    @staticmethod
    def unserialise(file, from_dict):
        name = from_dict(file)
        value_type = VALUE_TYPE_ORDER[_read_struct(_fmt_8bit, file)[0]]
        return IODef(name, value_type)


class _EntityView(Mapping[str, T]):
    """Provides a view over entity keyvalues, inputs, or outputs."""
    __slots__ = ['_ent', '_attr', '_disp_attr',]

    # Note, we expect the maps to have casefolded their keys.

    def __init__(self, ent: 'EntityDef', attr_name: str, disp_name: str):
        self._ent = ent
        self._attr = attr_name
        self._disp_attr = disp_name
        
    @property
    def __name__(self):
        return self._disp_attr
        
    def __repr__(self):
        return '{!r}.{}'.format(self._ent, self._disp_attr)

    def __eq__(self, other) -> bool:
        """We're private, so we should be the only instance for a given Entity."""
        return other is self
        
    def _maps(self, ent=None) -> Mapping[str, T]:
        """Yield all the mappings which we need to look through."""
        if ent is None:
            ent = self._ent
            
        yield getattr(ent, self._attr)
        for base in ent.bases:
            yield from self._maps(base)

    def __getitem__(self, name: str) -> T:
        fname = name.casefold()
        for ent_map in self._maps():
            try:
                return ent_map[fname]
            except KeyError:
                pass
        raise KeyError(name)

    def __contains__(self, name: str) -> bool:
        fname = name.casefold()
        for ent_map in self._maps():
            if fname in ent_map:
                return True
        return False
        
    def __iter__(self) -> Iterator[T]:
        seen = set()
        for ent_map in self._maps():
            for name in ent_map.keys():
                if name in seen:
                    continue
                seen.add(name)
                yield name
            
    def __len__(self) -> int:
        seen = set()
        for ent_map in self._maps():
            seen.update(ent_map)
        return len(seen)


# Fix a bug in some typing versions - slots can't be used with generics.
del _EntityView.__slots__

# Cache the classes ourselves here.
_Ent_View_KV = _EntityView[KeyValues]
_Ent_View_IO = _EntityView[IODef]

class EntityDef:
    """A definition for an entity."""
    def __init__(self, type: EntityTypes):
        self.type = type
        self.classname = ''
        self.keyvalues = {}
        self.inputs = {}
        self.outputs = {}
        # Base type names - base()
        self.bases = []
        # line(), studio(), etc in the header
        # this is a func, args tuple.
        self.helpers = []
        self.desc = []
        
        # Views for accessing data among all the entities.
        self.kv = _Ent_View_KV(self, 'keyvalues', 'kv')
        self.inp = _Ent_View_IO(self, 'inputs', 'inp')
        self.out = _Ent_View_IO(self, 'outputs', 'out')

    @classmethod
    def parse(
        cls,
        fgd: 'FGD',
        tok: Tokenizer,
        ent_type: EntityTypes,
    ):
        """Parse an entity definition."""
        entity = cls(ent_type)

        # First parse the bases part - lots of name(args) sections until an '='
        help_type = None
        for token, token_value in tok:
            if token is Token.NEWLINE:
                continue
            if token is Token.STRING:
                if help_type is None:
                    try:
                        help_type = HelperTypes(token_value)
                    except ValueError:
                        raise tok.error(
                            'Unknown HelperType "{}"!',
                            token_value,
                        )
                    continue
                else:
                    # No arguments for the previous helper - add it in like that.
                    entity.helpers.append((help_type, ''))

            elif token is Token.PAREN_ARGS:
                if help_type is None:
                    raise tok.error('Args without helper type! ({!r})', token_value)

                args = _RE_HELPER_ARGS.split(token_value)

                if help_type is HelperTypes.INHERIT:
                    for base in args:
                        base = base.casefold()
                        if base not in entity.bases:
                            entity.bases.append(base.strip())
                    help_type = None
                    continue

                entity.helpers.append((help_type, args))

                help_type = None

            elif token is Token.EQUALS:
                break
            else:
                raise tok.error(token)
        else:
            raise tok.error('Entity header never ended!')

        # We were waiting for arguments for the previous helper.
        # We need to add with none.
        if help_type:
            entity.helpers.append((help_type, ''))

        entity.classname = tok.expect(Token.STRING).strip()

        # We next might have a ':' then docstring before the [,
        # or directly to [.
        desc = None
        for doc_token, token_value in tok:
            if doc_token is Token.NEWLINE:
                continue
            if doc_token is Token.COLON:
                if desc is None:
                    desc = []
                else:
                    raise tok.error('Two colons in entity description!')
            elif doc_token is Token.STRING:
                if desc is None or desc:
                    # No colon yet, or we have text without '+' between
                    raise tok.error(doc_token)
                desc.append(token_value)
            elif doc_token is Token.PLUS:
                if not desc:
                    raise tok.error('+ without string before it!')
                desc.append(tok.expect(Token.STRING))
            elif doc_token is Token.BRACK_OPEN:
                if desc:
                    entity.desc = ''.join(desc)
                break
            else:
                raise tok.error(doc_token)

        fgd.entities[entity.classname.casefold()] = entity

        # Now parse keyvalues, and input/outputs
        for token, token_value in tok:
            if token is Token.BRACK_CLOSE:
                break  # End of this entity.

            if token is Token.NEWLINE:
                continue

            # IO - keyword at the start.
            if token is not Token.STRING:
                raise tok.error(token)

            io_type = token_value.casefold()
            if io_type in ('input', 'output'):

                name = tok.expect(Token.STRING)
                raw_value_type = tok.expect(Token.PAREN_ARGS).strip()
                try:
                    val_typ = VALUE_TYPE_LOOKUP[raw_value_type.casefold()]
                except KeyError:
                    raise tok.error('Unknown keyvalue type "{}"!', raw_value_type)

                # Can't have a spawnflags or choices input type...
                if val_typ.has_list:
                    raise tok.error(
                        '"{}" value type is not valid for an input or output!',
                        val_typ.value,
                    )

                # Read desc
                attrs, token = read_colon_list(tok)

                if token is token.EQUALS:
                    raise tok.error(token)

                if attrs:
                    try:
                        [desc] = attrs
                    except ValueError:
                        raise tok.error('Too many values for IO definition!')
                else:
                    desc = ''

                # entity.inputs or entity.outputs
                getattr(entity, io_type + 's')[name] = IODef(name, val_typ, desc)

            else:
                # Keyvalue
                name = io_type

                raw_value_type = tok.expect(Token.PAREN_ARGS).strip()
                try:
                    val_typ = VALUE_TYPE_LOOKUP[raw_value_type.casefold()]
                except KeyError:
                    raise tok.error('Unknown keyvalue type "{}"!', raw_value_type)

                next_token, key_flag = tok()

                is_readonly = False
                had_colon = False
                attrs = None

                if next_token is Token.STRING:
                    # 'report' or 'readonly'
                    if key_flag.casefold() == 'readonly':
                        is_readonly = True
                elif next_token is Token.COLON:
                    had_colon = True
                elif next_token is Token.EQUALS:
                    # Special case - spawnflags doesn't have to have
                    # any info - skips straight to the end.
                    if val_typ is ValueTypes.SPAWNFLAGS:
                        attrs = []
                        has_equal = next_token
                elif next_token is Token.NEWLINE:
                    attrs = []
                    has_equal = next_token
                else:
                    raise tok.error(next_token)

                if attrs is None:
                    attrs, has_equal = read_colon_list(tok, had_colon)
                attr_len = len(attrs)

                desc = ''
                default = None
                if attr_len == 3:
                    disp_name, default, desc = attrs
                elif attr_len == 2:
                    disp_name, default = attrs
                elif attr_len == 1:
                    [disp_name] = attrs
                elif attr_len == 0:
                    disp_name = name
                else:
                    raise tok.error('Too many attributes for keyvalue!\n{!r}', attrs)

                if val_typ.has_list:
                    if has_equal is not Token.EQUALS:
                        raise tok.error('No list for "{}" value type!', val_typ.name)
                    # Read the choices in the []
                    val_list = []
                    tok.expect(Token.BRACK_OPEN)
                    for choices_token, choices_value in tok:
                        if choices_token is Token.NEWLINE:
                            continue
                        if choices_token is Token.BRACK_CLOSE:
                            break
                        elif choices_token is not Token.STRING:
                            raise tok.error(choices_token)
                        vals, has_equal = read_colon_list(tok, had_colon=False)
                        
                        if val_typ is ValueTypes.SPAWNFLAGS:
                            # The first value is an integer.
                            try:
                                choices_value = int(choices_value)
                            except ValueError:
                                raise tok.error(
                                    'SpawnFlags must be integer values, '
                                    'not "{}" (in {})!'.format(
                                        choices_value, 
                                        entity.classname, 
                                    )
                                ) from None
                            power = math.log2(choices_value)
                            if power != round(power):
                                raise tok.error(
                                    'SpawnFlags must be powers of two, '
                                    'not {} (in {})!'.format(
                                        choices_value,
                                        entity.classname,
                                    )
                                ) from None
                            

                        # Spawnflags can have a default, others don't
                        if len(vals) == 2 and val_typ is ValueTypes.SPAWNFLAGS:
                            val_list.append((choices_value, vals[0], bool(vals[1])))
                        elif len(vals) == 1:
                            if val_typ is ValueTypes.SPAWNFLAGS:
                                val_list.append((choices_value, vals[0], True))
                            else:
                                val_list.append((choices_value, vals[0]))
                        elif len(vals) == 0:
                            raise tok.error(Token.STRING)
                        else:
                            raise tok.error('Too many values!\n{}', vals)

                        # Handle ] at the end of a : : line.
                        if has_equal is Token.BRACK_CLOSE:
                            break
                    else:
                        raise tok.error(token.EOF)
                else:
                    val_list = None
                    if has_equal is Token.EQUALS:
                        raise tok.error('"{}" value types can\'t have lists!', val_typ.name)

                entity.keyvalues[name.casefold()] = KeyValues(
                    name,
                    val_typ,
                    disp_name,
                    default,
                    desc,
                    val_list,
                    is_readonly == 'readonly',
                )

    def __repr__(self):
        if self.type is EntityTypes.BASE:
            return '<Entity Base "{}">'.format(self.classname)
        else:
            return '<Entity {}>'.format(self.classname)
            
    def serialise(self, file, str_dict: BinStrDict):
        """Write to the binary file."""
        file.write(_fmt_ent_header.pack(
            ENTITY_TYPE_INDEX[self.type],
            len(self.bases),
            len(self.keyvalues),
            len(self.inputs),
            len(self.outputs),
        ))
        file.write(str_dict(self.classname))
        
        for base_ent in self.bases:
            file.write(str_dict(base_ent.classname))
        
        for kv in self.keyvalues.values():
            kv.serialise(file, str_dict)
            
        for inp in self.inputs.values():
            inp.serialise(file, str_dict)
            
        for out in self.outputs.values():
            out.serialise(file, str_dict)
        
        # Helpers are not added.
        
    @staticmethod
    def unserialise(file, from_dict) -> 'EntityDef':
        """Read from the binary file."""
        [
            type_ind,
            base_count,
            kv_count,
            inp_count,
            out_count,
        ] = _read_struct(_fmt_ent_header, file)
        
        ent = EntityDef(ENTITY_TYPE_ORDER[type_ind])
        ent.classname = from_dict(file)
        ent.desc = ''
        
        for _ in range(base_count):
            ent.bases.append(from_dict(file))
            
        for _ in range(kv_count):
            kv = KeyValues.unserialise(file, from_dict)
            ent.keyvalues[kv.name] = kv
            
        for _ in range(inp_count):
            inp = IODef.unserialise(file, from_dict)
            ent.inputs[inp.name] = inp
            
        for _ in range(out_count):
            out = IODef.unserialise(file, from_dict)
            ent.outputs[out.name] = out
           
        return ent 


class FGD:
    """A FGD set for a game. May be composed of several files."""
    def __init__(self):
        """Create a FGD."""
        # List of names we have already parsed.
        # We don't parse them again, to prevent infinite loops.
        self._parse_list = []
        # Entity definitions
        self.entities = {}  # type: Dict[str, EntityDef]
        # maximum bounding box of map
        self.map_size_min = 0
        self.map_size_max = 0

    @classmethod
    def parse(
        cls,
        file: Union[File, str],
        filesystem: FileSystem=None,
    ) -> 'FGD':
        """Parse an FGD file.

        Parameters:
        * file: A filesys.File representing the file to read, or a file path.
        * filesystem: The system to lookup files in. This is needed to 
          resolve file inclusions. If not passed, file must be a filesystem
          File to obtain a matching filesystem.
        """
        if filesystem is not None and not isinstance(file, File):
            if not file.endswith('.fgd'):
                file += '.fgd'
            try:
                with filesystem:
                    file = filesystem[file]
            except KeyError:
                raise FileNotFoundError(file)
        elif isinstance(file, File):
            filesystem = file.sys
        else:
            raise TypeError(
                'String file path passed ({!r}), but no filesystem!'.format(file)
            )
        fgd = cls()
        fgd._parse_file(filesystem, file)
        fgd._apply_bases()
        return fgd

    def _apply_bases(self):
        """Fix base values in entities after parsing.
        
        While parsing the classnames are set as strings,
        so order in the file doesn't matter. This fixes
        them to the real entity objects.
        """
        for ent in self:
            orig_bases = ent.bases
            new_bases = ent.bases = []
            for base in orig_bases:
                if isinstance(base, EntityDef):
                    # This entity was already done.
                    new_bases.append(base)
                    continue
                
                try:
                    new_bases.append(self[base])
                except KeyError:
                    raise ValueError(
                        'Unknown base ({}) for {}'.format(
                            base,
                            ent.classname,
                        )
                    )


    def _parse_file(self, filesys: FileSystem, file: File):
        """Parse one file (recursively if needed)."""

        if file in self._parse_list:
            return

        self._parse_list.append(file)

        with filesys, file.open_str() as f:
            tokeniser = Tokenizer(
                f,
                filename=file.path,
                error=FGDParseError,
                string_bracket=False,
            )
            for token, token_value in tokeniser:
                # The only things at top-level would be bare strings, and empty lines.
                if token is Token.NEWLINE:
                    continue
                if token is not Token.STRING:
                    raise tokeniser.error(token)
                token_value = token_value.casefold()

                if token_value == '@include':
                    include_file = tokeniser.expect(Token.STRING)
                    if not include_file.endswith('.fgd'):
                        include_file += '.fgd'

                    try:
                        include = filesys[include_file]
                    except KeyError:
                        raise FileNotFoundError(file)
                    self._parse_file(filesys, include)

                elif token_value == '@mapsize':
                    # Max/min map size definition
                    mapsize_args = tokeniser.expect(Token.PAREN_ARGS)
                    try:
                        min_size, max_size = mapsize_args.split(',')
                        self.map_size_min = int(min_size.strip())
                        self.map_size_max = int(max_size.strip())
                    except ValueError:
                        raise tokeniser.error(
                            'Invalid @MapSize: ({})',
                            mapsize_args,
                        )
                # Entity definition...
                elif token_value[:1] == '@':
                    try:
                        ent_type = EntityTypes(token_value[1:])
                    except ValueError:
                        raise tokeniser.error(
                            'Invalid Entity type "{}"!',
                            token_value[1:],
                        )
                    EntityDef.parse(self, tokeniser, ent_type)
                else:
                    raise tokeniser.error('Bad keyword {!r}', token_value)

    def __getitem__(self, classname) -> EntityDef:
        try:
            return self.entities[classname.casefold()]
        except KeyError:
            raise KeyError('No class "{}"!'.format(classname)) from None

    def __iter__(self) -> Iterator[EntityDef]:
        return iter(self.entities.values())

    def serialise(self, file):
        """Write the FGD into a compacted binary format.
        
        This is only readable by this module, and does not contain
        entity, keyvalue and IO help descriptions to keep the data small.
        """
        # The start of a file is a list of all used strings. 
        dictionary = BinStrDict()
        
        # Start of file - format version, FGD min/max, number of entities.
        file.write(b'FGD' + _fmt_header.pack(
            BIN_FORMAT_VERSION,
            self.map_size_min,
            self.map_size_max,
            len(self.entities),
        ))
        
        ent_data = io.BytesIO()
        for ent in self.entities.values():
            ent.serialise(ent_data, dictionary)
            
        # The final file is the header, dictionary data, and all the entities 
        # one after each other.
        dictionary.serialise(file)
        file.write(ent_data.getvalue())
      
    @classmethod  
    def unserialise(cls, file) -> 'FGD':
        """Unpack data from FGD.serialse() to return the original data.
        
        Help descriptions are not preserved, and are set to <BINARY>.
        """
        
        if file.read(3) != b'FGD':
            raise ValueError('Not an FGD file!')
        
        fgd = FGD()
        
        [
            format_version,
            fgd.map_size_min,
            fgd.map_size_max,
            ent_count,
        ] = _read_struct(_fmt_header, file)
        
        if format_version > BIN_FORMAT_VERSION:
            raise TypeError('Unknown format version "{}"!'.format(format_version))
            
        from_dict = BinStrDict.unserialise(file)

        # Now there's ent_count entities after each other.
        for _ in range(ent_count):
            ent = EntityDef.unserialise(file, from_dict)
            fgd.entities[ent.classname.casefold()] = ent
        
        fgd._apply_bases()
        return fgd
