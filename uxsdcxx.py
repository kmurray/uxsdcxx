# Script to convert an XSD schema to a C++ struct definition and
# a parser based on PugiXML.

from pprint import pprint

import os
import re
import sys
import xmlschema
from xmlschema.validators import (
    XsdAttribute,
    XsdAtomicBuiltin,
    XsdAtomicRestriction,
    XsdComplexType,
    XsdElement,
    XsdGroup,
    XsdSimpleType,
    XsdList,
    XsdType,
    XsdUnion
)

from itertools import chain
# Note that Python type annotations are just annotations and
# have no runtime effects. But they are useful for documentation.
from typing import List, Tuple, Dict, Set, Union

from dfa import dfa_from_group
from third_party import triehash

xs = "{http://www.w3.org/2001/XMLSchema}"
atomic_builtins = {
	xs+"string": "const char *",
	xs+"boolean": "bool",
	xs+"float": "float",
	xs+"decimal": "int",
	xs+"integer": "int",
	xs+"nonPositiveInteger": "int",
	xs+"negativeInteger": "int",
	xs+"long": "long",
	xs+"int": "int",
	xs+"short": "short",
	xs+"byte": "char",
	xs+"nonNegativeInteger": "unsigned int",
	xs+"unsignedLong": "unsigned long",
	xs+"unsignedInt": "unsigned int",
	xs+"unsignedShort": "unsigned short",
	xs+"unsignedByte": "unsigned byte",
	xs+"positiveInteger": "unsigned int",
	xs+"double": "double",
}

# TODO: Make actual validators for these.
atomic_builtin_load_formats = {
	xs+"string": "strdup(%s)",
	xs+"boolean": "std::strtol(%s, NULL, 10)",
	xs+"float": "std::strtof(%s, NULL)",
	xs+"decimal": "std::strtol(%s, NULL, 10)",
	xs+"integer": "std::strtol(%s, NULL, 10)",
	xs+"nonPositiveInteger": "std::strtol(%s, NULL, 10)",
	xs+"negativeInteger": "std::strtol(%s, NULL, 10)",
	xs+"long": "std::strtoll(%s, NULL, 10)",
	xs+"int": "std::strtol(%s, NULL, 10)",
	xs+"short": "std::strtol(%s, NULL, 10)",
	xs+"byte": "std::strtol(%s, NULL, 10)",
	xs+"nonNegativeInteger": "std::strtoul(%s, NULL, 10)",
	xs+"unsignedLong": "std::strtoull(%s, NULL, 10)",
	xs+"unsignedInt": "std::strtoul(%s, NULL, 10)",
	xs+"unsignedShort": "std::strtoul(%s, NULL, 10)",
	xs+"unsignedByte": "std::strtoul(%s, NULL, 10)",
	xs+"positiveInteger": "std::strtoul(%s, NULL, 10)",
	xs+"double": "std::strtod(%s, NULL)",
}

cpp_keywords = ["alignas", "alignof", "and", "and_eq", "asm", "atomic_cancel", "atomic_commit", "atomic_noexcept",
			"auto", "bitand", "bitor", "bool", "break", "case", "catch", "char", "char8_t", "char16_t", "char32_t", "class",
			"compl", "concept", "const", "consteval", "constexpr", "const_cast", "continue", "co_await", "co_return",
			"co_yield", "decltype", "default", "delete", "do", "double", "dynamic_cast", "else", "enum", "explicit",
			"export", "extern", "false", "float", "for", "friend", "goto", "if", "inline", "int", "long", "mutable",
			"namespace", "new", "noexcept", "not", "not_eq", "nullptr", "operator", "or", "or_eq", "private",
			"protected", "public", "reflexpr", "register", "reinterpret_cast", "requires", "return", "short", "signed",
			"sizeof", "static", "static_assert", "static_cast", "struct", "switch", "synchronized", "template", "this",
			"thread_local", "throw", "true", "try", "typedef", "typeid", "typename", "union", "unsigned", "using",
			"virtual", "void", "volatile", "wchar_t", "while", "xor", "xor_eq"]

#

builtin_fn_declarations = """
void dfa_error(const char *wrong, int *states, const char **lookup, int len);
template<std::size_t N>
void all_error(std::bitset<N> gstate, const char **lookup);
template<std::size_t N>
void attr_error(std::bitset<N> astate, const char **lookup);
void alloc_arenas(void);
void get_root_elements(const char *filename);
"""

dfa_error_fn = """
/* runtime error for <xs:choice> or <xs:sequence>s */
void dfa_error(const char *wrong, int *states, const char **lookup, int len){
	std::vector<std::string> expected;
	for(int i=0; i<len; i++){
		if(states[i] != -1) expected.push_back(lookup[i]);
	}

	std::string expected_or = expected[0];
	for(unsigned int i=1; i<expected.size(); i++)
		expected_or += std::string(" or ") + expected[i];

	throw std::runtime_error("Expected " + expected_or + ", found " + std::string(wrong));
}
"""

attr_error_fn = """
/* runtime error for attributes */
template<std::size_t N>
void attr_error(std::bitset<N> astate, const char **lookup){
	std::vector<std::string> missing;
	for(unsigned int i=0; i<N; i++){
		if(astate[i] == 0) missing.push_back(lookup[i]);
	}

	std::string missing_and = missing[0];
	for(unsigned int i=1; i<missing.size(); i++)
		missing_and += std::string(", ") + missing[i];

	throw std::runtime_error("Didn't find required attributes " + missing_and + ".");
}
"""

all_error_fn = """
/* runtime error for <xs:all>s */
template<std::size_t N>
void all_error(std::bitset<N> gstate, const char **lookup){
	std::vector<std::string> missing;
	for(unsigned int i=0; i<N; i++){
		if(gstate[i] == 0) missing.push_back(lookup[i]);
	}

	std::string missing_and = missing[0];
	for(unsigned int i=1; i<missing.size(); i++)
		missing_and += std::string(", ") + missing[i];

	throw std::runtime_error("Didn't find required elements " + missing_and + ".");
}
"""

#

filename = sys.argv[1]
namespace = os.path.splitext(os.path.basename(filename))[0]

# Read in the schema- here we go!
schema = xmlschema.validators.XMLSchema10(filename)

# Complex types found inside elements. They are not found in the global map,
# so we have to reserve them while traversing types in the global map
# and generate them afterwards.
anonymous_complex_types = []

# Enumerations and unions found inside elements.
enums = []
unions = []

# Simple types found inside unions.
# We generate a special "types" enum from this, to put in all tagged union type definitions.
simple_types = set()

# In C++ code, we have to allocate global arenas for types which can occur more than once,
# so that we can avoid lots of reallocs on the heap.
arena_types = set()

# Get all global user-defined types.
types: List[XsdComplexType] = [v for k, v in schema.types.items() if "w3.org" not in k and isinstance(v, XsdComplexType)]
elements: List[XsdElement] = [v for v in schema.elements.values()]

# Check against keywords, warn and rename if necessary.
def checked(x: str) -> str:
	if x in cpp_keywords:
		print("%s is a C++ keyword. Changing it to %s_." % (x, x), file=sys.stderr)
		return x + "_"
	return x

# Convert type name to C++ type. Elements can have builtins as types.
def to_cpp_type(x: str) -> str:
	if x in atomic_builtins: return atomic_builtins[x]
	else: return "t_%s" % x

def to_token(x: str) -> str:
	return re.sub(r"[^a-zA-Z0-9_]", "_", x).upper()

def to_union_member_type(x: str) -> str:
	return "as_%s" % re.sub(r"[^a-zA-Z0-9_]", "_", x)

def indent(x: str, n: int=1) -> str:
	return "\n".join(["\t"*n + line if line else "" for line in x.split("\n")])

#

# Annotate the xmlschema tree with convenient data structures, such as ordered
# access to children and attributes, C++ types of complex types etc.
def anno_type_element(t: XsdElement, many=False, optional=False) -> None:
	if getattr(t, "cpp_type", None) is not None: return

	if t.occurs[1] is None or t.occurs[1] > 1: many = True
	if t.occurs[0] is 0: optional = True
	if not t.type.name:
		t.type.name = t.name
		anonymous_complex_types.append(t.type)

	if isinstance(t.type, XsdComplexType):
		anno_type_complex_type(t.type)
		# We have a recursive type and anno_typedecl_complex_type did nothing.
		if not getattr(t.type, "cpp_type", None): return
	else:
		anno_type_simple_type(t.type)

	t.many = many
	t.optional = optional
	if many: arena_types.add(t.type)
	t.cpp_type = t.type.cpp_type

# Return a list of elements in the group to aid complex type annotation.
def anno_type_group(t: XsdGroup, many=False, optional=False) -> List[XsdElement]:
	out = []
	if t.occurs[1] is None or t.occurs[1] > 1: many = True
	if t.occurs[0] is 0: optional = True
	for e in t._group:
		if isinstance(e, XsdGroup):
			out += anno_type_group(e, many, optional)
		elif isinstance(e, XsdElement):
			out += [e]
			anno_type_element(e, many, optional)
		else:
			raise NotImplementedError("I don't know what to do with group member %s." % e)
	return out

# Only enumerations are supported as restrictions.
def anno_type_restriction(t: XsdAtomicRestriction) -> None:
	assert len(t.validators) == 1, "I can only handle simple enumerations."
	# Possibly member of an XsdList or XsdUnion.
	if not t.name: t.name = t.parent.name
	t.cpp_type = "enum_%s" % t.name
	enums.append(t)

def anno_type_union(t: XsdUnion) -> None:
	t.cpp_type = "union_%s" % t.name
	for m in t.member_types:
		anno_type_simple_type(m)
		simple_types.add(m)
	unions.append(t)

# See https://www.obj-sys.com/docs/xbv23/CCppUsersGuide/ch04.html.
def anno_type_simple_type(t: XsdSimpleType) -> None:
	if isinstance(t, XsdAtomicBuiltin):
		t.cpp_type = atomic_builtins[t.name]
	elif isinstance(t, XsdAtomicRestriction):
		anno_type_restriction(t)
	# Just read xs:lists into a string for now.
	# That simplifies validation and keeps heap allocation to nodes only.
	# VPR just reads list types into a string, too.
	elif isinstance(t, XsdList):
		t.cpp_type = "const char *"
	elif isinstance(t, XsdUnion):
		anno_type_union(t)
	else:
		raise NotImplementedError("I don't know what to do with type %s." % t)

def anno_type_complex_type(t: XsdComplexType) -> None:
	if getattr(t, "cpp_type", None) is not None: return
	t.cpp_type = to_cpp_type(t.name)

	# https://stackoverflow.com/a/39835527
	t.attribute_list = list(dict.fromkeys(t.attributes.values()))
	for attr in t.attribute_list:
		assert attr.use != "prohibited"
		anno_type_simple_type(attr.type)

	t.model = None
	t.child_elements = []
	if isinstance(t.content_type, XsdGroup) and len(t.content_type._group) > 0:
		if t.content_type.model == "all":
			t.model = "all"
		elif t.content_type.model in ["choice", "sequence"]:
			t.model = "dfa"
			t.dfa = dfa_from_group(t.content_type)
		else:
			raise NotImplementedError("Model group %s is not supported." % t.content_type.model)
		t.child_elements = anno_type_group(t.content_type)
	if t.has_simple_content():
		anno_type_simple_type(t.content_type)

# Annotate the schema with cpp_types of every complex and simple type.
# Put aside anonymous complex types, enums and unions for generating actual type declaration.
for t in types:
	anno_type_complex_type(t)

for e in elements:
	anno_type_element(e)

# Sort types by tree height.
def key_ctype(x: XsdComplexType, visited=None):
	if not visited: visited=set()
	if x in visited: return 0
	else: visited.add(x)
	return max([0] + [key_ctype(y.type, visited) for y in x.child_elements]) + 1

types += anonymous_complex_types
types.sort(key=key_ctype)

#

# Generate a tagged union type.
# "types" is a reserved enum which is generated from the atomic builtins.
def typedefn_from_union(t: XsdUnion) -> str:
	out = "struct %s {\n" % t.cpp_type
	out += "\ttype_tag tag;\n"
	out += "\tunion {\n"
	for e in t.member_types:
		out += "\t\t%s %s;\n" % (e.cpp_type, to_union_member_type(e.cpp_type))
	out += "\t};\n"
	out += "};\n"
	return out

def typedefn_from_complex_type(t: XsdComplexType) -> str:
	out = ""
	for attr in t.attribute_list:
		out += "%s %s;\n" % (attr.type.cpp_type, checked(attr.name))
	for child in t.child_elements:
		if child.many:
			out += "int num_%s;\n" % child.name
			out += "%s * %s_list;\n" % (child.cpp_type, child.name)
		else:
			if child.optional:
				out += "bool has_%s;\n" % child.name
			out += "%s %s;\n" % (child.cpp_type, checked(child.name))
	if t.has_simple_content():
		out += "%s value;\n" % (t.content_type.cpp_type)

	out = "struct %s {\n" % t.cpp_type + indent(out) + "};\n"
	return out

struct_declarations = ""
struct_definitions = ""
for t in types:
	struct_declarations += "struct %s;\n" % t.cpp_type
	struct_definitions += typedefn_from_complex_type(t)

unions = list(dict.fromkeys(unions))
union_definitions = ""
for u in unions:
	struct_declarations += "struct %s;\n" % u.cpp_type
	union_definitions += typedefn_from_union(u) + "\n"

element_definitions = ""
for e in elements:
	element_definitions += "%s %s = {};\n" % (e.cpp_type, e.name)

# Put the types found so far in unions in an enum. This is our enum of type tags.
simple_types = sorted(simple_types, key=lambda x: x.name)
type_tag_definition = "enum class type_tag {%s};\n" % ", ".join(set([to_token(t.cpp_type) for t in simple_types]))

count_fn_declarations = ""
for t in types:
	count_fn_declarations += "void count_%s(const pugi::xml_node &root);\n" % t.name

load_fn_declarations = ""
for t in types:
	load_fn_declarations += "void load_%s(const pugi::xml_node &root, %s *out);\n" % (t.name, t.cpp_type)

arena_declarations = ""
for t in sorted(arena_types, key=lambda x: x.name):
	arena_declarations += "int g_num_%s = 0;\n" % t.name
	arena_declarations += "%s *%s_arena;\n" % (t.cpp_type, t.name)

#

def tokens_from_enum(t: XsdAtomicRestriction) -> str:
	out = ""
	enum_tokens = ["UXSD_INVALID = 0"]
	enum_tokens += [to_token(x) for x in t.validators[0].enumeration]
	lookup_tokens = ["\"UXSD_INVALID\""]
	lookup_tokens += ["\"%s\"" % x for x in t.validators[0].enumeration]
	out += "enum class %s {%s};\n" % (t.cpp_type, ", ".join(enum_tokens))
	out += "const char *lookup_%s[] = {%s};\n" % (t.name, ", ".join(lookup_tokens))
	return out

def lexer_from_enum(t: XsdAtomicRestriction) -> str:
	assert t.cpp_type.startswith("enum")
	out = ""
	out += "inline %s lex_%s(const char *in, bool throw_on_invalid){\n" % (t.cpp_type, t.name)
	triehash_alph = [(x, "%s::%s" % (t.cpp_type, to_token(x))) for x in t.validators[0].enumeration]
	out += indent(triehash.gen_lexer_body(triehash_alph))
	out += "\tif(throw_on_invalid)\n"
	out += "\t\tthrow std::runtime_error(\"Found unrecognized enum value \" + std::string(in) + \"of %s.\");\n" % t.cpp_type
	out += "\treturn %s::UXSD_INVALID;\n" % t.cpp_type
	out += "}\n"
	return out

def tokens_from_complex_type(t: XsdComplexType) -> str:
	out = ""
	if t.child_elements:
		enum_tokens = [to_token(e.name) for e in t.child_elements]
		lookup_tokens = ["\"%s\"" % e.name for e in t.child_elements]
		out += "enum class gtok_%s {%s};\n" % (t.cpp_type, ", ".join(enum_tokens))
		out += "const char *gtok_lookup_%s[] = {%s};\n" % (t.cpp_type, ", ".join(lookup_tokens))
	if t.attributes:
		enum_tokens = [to_token(x.name) for x in t.attribute_list]
		lookup_tokens = ["\"%s\"" % x.name for x in t.attribute_list]
		out += "enum class atok_%s {%s};\n" % (t.cpp_type, ", ".join(enum_tokens))
		out += "const char *atok_lookup_%s[] = {%s};\n" % (t.cpp_type, ", ".join(lookup_tokens))
	return out

def lexer_from_complex_type(t: XsdComplexType) -> str:
	out = ""
	if t.child_elements:
		out += "inline gtok_%s glex_%s(const char *in){\n" % (t.cpp_type, t.cpp_type)
		triehash_alph = [(e.name, "gtok_%s::%s" % (t.cpp_type, to_token(e.name))) for e in t.child_elements]
		out += indent(triehash.gen_lexer_body(triehash_alph))
		out += "\tthrow std::runtime_error(\"Found unrecognized child \" + std::string(in) + \" of <%s>.\");\n" % t.name
		out += "}\n"
	if t.attributes:
		out += "inline atok_%s alex_%s(const char *in){\n" % (t.cpp_type, t.cpp_type)
		triehash_alph = [(x.name, "atok_%s::%s" % (t.cpp_type, to_token(x.name))) for x in t.attribute_list]
		out += indent(triehash.gen_lexer_body(triehash_alph))
		out += "\tthrow std::runtime_error(\"Found unrecognized attribute \" + std::string(in) + \" of <%s>.\");\n" % t.name
		out += "}\n"
	return out

enums = list(dict.fromkeys(enums))

enum_tokens = ""
for e in enums:
	enum_tokens += tokens_from_enum(e)

enum_lexers = ""
for e in enums:
	enum_lexers += lexer_from_enum(e) + "\n"

complex_type_tokens = ""
for t in types:
	complex_type_tokens += tokens_from_complex_type(t)

complex_type_lexers = ""
for t in types:
	complex_type_lexers += lexer_from_complex_type(t) + "\n"

#

def _gen_dfa_table(t: XsdGroup) -> str:
	out = ""
	out += "int gstate_%s[%d][%d] = {\n" % (t.cpp_type, len(t.dfa.states), len(t.dfa.alphabet))
	for i in range(0, max(t.dfa.states)+1):
		state = t.dfa.transitions[i]
		row = [str(state[x]) if state.get(x) is not None else "-1" for x in t.dfa.alphabet]
		out += "\t{%s},\n" % ", ".join(row)
	out += "};\n"
	return out

def _gen_count_dfa(t: XsdGroup) -> str:
	out = ""
	out += "int next, state=%d;\n" % t.dfa.start
	out += "for(pugi::xml_node node = root.first_child(); node; node = node.next_sibling()){\n"
	out += "\tgtok_%s in = glex_%s(node.name());\n" % (t.cpp_type, t.cpp_type)
	out += "\tnext = gstate_%s[state][(int)in];\n" % t.cpp_type
	out += "\tif(next == -1) dfa_error(gtok_lookup_%s[(int)in], gstate_%s[state], gtok_lookup_%s, %d);\n"  % (t.cpp_type, t.cpp_type, t.cpp_type, len(t.dfa.alphabet))
	out += "\tstate = next;\n"

	out += "\tswitch(in){\n";
	for el in t.child_elements:
		if not isinstance(el.type, XsdComplexType): continue
		out += "\tcase gtok_%s::%s:\n" % (t.cpp_type, to_token(el.name))
		out += "\t\tcount_%s(node);\n" % el.type.name
		if el.many: out += "\t\tg_num_%s++;\n" % el.type.name
		out += "\t\tbreak;\n"
	out += "\tdefault: break; /* Not possible. */\n"
	out += "\t}\n";

	reject_cond = " && ".join(["state != %d" % x for x in t.dfa.accepts])
	out += "}\n"
	out += "if(%s) dfa_error(\"end of input\", gstate_%s[state], gtok_lookup_%s, %d);\n" % (reject_cond, t.cpp_type, t.cpp_type, len(t.dfa.alphabet))
	return out

# Mostly the same thing with _gen_load_attr.
# Looks like it's planned in XSD 1.0 for alls to be implemented in the same way
# with attributes.
def _gen_count_all(t: XsdGroup) -> str:
	out = ""
	N = len(t.child_elements)
	out += "std::bitset<%d> gstate = 0;\n" % N
	out += "for(pugi::xml_node node = root.first_child(); node; node = node.next_sibling()){\n"
	out += "\tgtok_%s in = glex_%s(node.name());\n" % (t.cpp_type, t.cpp_type)
	out += "\tif(gstate[(int)in] == 0) gstate[(int)in] = 1;\n"
	out += "\telse throw std::runtime_error(\"Duplicate element \" + std::string(node.name()) + \" in <%s>.\");\n" % t.name

	out += "\tswitch(in){\n";
	for el in t.child_elements:
		out += "\tcase gtok_%s::%s:\n" % (t.cpp_type, to_token(el.name))
		out += "\t\tcount_%s(node);\n" % el.type.name
		out += "\t\tbreak;\n"
	out += "\tdefault: break; /* Not possible. */\n"
	out += "\t}\n";
	out += "}\n"

	mask = "".join(["1" if x.occurs[0] == 0 else "0" for x in t.child_elements][::-1])
	out += "std::bitset<%d> test_state = gstate | std::bitset<%d>(0b%s);\n" % (N, N, mask)
	out += "if(!test_state.all()) all_error(test_state, gtok_lookup_%s);\n" % t.cpp_type
	return out

def count_fn_from_complex_type(t: XsdComplexType) -> str:
	out = ""
	out += "void count_%s(const pugi::xml_node &root){\n" % t.name
	if t.model == "all":
		out += indent(_gen_count_all(t))
	elif t.model == "dfa":
		out = _gen_dfa_table(t) + out
		out += indent(_gen_count_dfa(t))
	else:
		out += "\tif(root.first_child().type() == pugi::node_element) throw std::runtime_error(\"Unexpected child element in <%s>.\");\n" % t.name
	out += "}\n"
	return out

count_fn_definitions = ""
for t in types:
	count_fn_definitions += count_fn_from_complex_type(t) + "\n"

#

# TODO: Find a cleaner way to load unions.
def _gen_load_union(t: XsdUnion, container: str, input: str) -> str:
	out = ""
	for m in t.member_types:
		new_container = "%s.%s" % (container, to_union_member_type(m.cpp_type))
		out += "%s.tag = type_tag::%s;\n" % (container, to_token(m.cpp_type))
		if isinstance(m, XsdAtomicBuiltin):
			out += "%s = %s;\n" % (new_container, atomic_builtin_load_formats[m.name] % input)
			out += "if(errno == 0)\n"
			out += "\tbreak;\n"
		elif isinstance(m, XsdAtomicRestriction):
			out += "%s = lex_%s(%s, false);\n" % (new_container, m.name, input)
			out += "if(%s != %s::UXSD_INVALID)\n" % (new_container, m.cpp_type)
			out += "break;\n"
		else:
			raise NotImplementedError("I don't know how to load %s into a union." % m)
	out += "throw std::runtime_error(\"Couldn't load a suitable value into union %s.\");\n" % t.name
	return out

# See https://stackoverflow.com/questions/26080829/detecting-strtol-failure
# Since detecting additional characters require some other hoops which would
# hurt performance, we only detect errors using errno.
def _gen_load_simple(t: XsdSimpleType, container: str, input: str) -> str:
	out = ""
	if isinstance(t, XsdAtomicBuiltin):
		out += "%s = %s;\n" % (container, atomic_builtin_load_formats[t.name] % input)
		out += "if(errno != 0)\n"
		out += "\tthrow std::runtime_error(\"Invalid value `\" + std::string(%s) + \"` to load a %s into %s.\");" % (input, t.cpp_type, container)
	elif isinstance(t, XsdAtomicRestriction):
		out += "%s = lex_%s(%s, true);\n" % (container, t.name, input)
	elif isinstance(t, XsdList):
		out += "%s = strdup(%s);\n" % (container, input)
	elif isinstance(t, XsdUnion):
		out += _gen_load_union(t, container, input)
	else:
		raise NotImplementedError("I don't know how to load %s." % t)
	return out

def _gen_load_element_complex(t: XsdElement, parent: str="") -> str:
	out = ""
	container = "%s%s" % ("%s->" % parent if parent else "", t.name)
	if t.many:
		out += "load_%s(node, &%s_arena[g_num_%s]);\n" % (t.type.name, t.type.name, t.type.name)
		out += "g_num_%s++;\n" % t.type.name
		out += "%s->num_%s++;\n" % (parent, t.name)
	else:
		out += "load_%s(node, &%s);\n" % (t.type.name, container)
		if t.optional:
			out += "%s->has_%s = 1;\n" % (parent, t.name)
	return out

def _gen_load_element(t: XsdElement, parent: str="") -> str:
	if isinstance(t.type, XsdComplexType):
		return _gen_load_element_complex(t, parent)
	elif isinstance(t.type, XsdAtomicBuiltin):
		container = "%s%s" % ("%s->" % parent if parent else "", t.name)
		return "%s = %s;\n" % (container, atomic_builtin_load_formats[t.type.name] % "node.child_value()")
	else:
		raise NotImplementedError("I don't know how to load %s." % t.type)

def _gen_load_group(t: XsdGroup) -> str:
	out = ""
	for el in [x for x in t.child_elements if x.many]:
		out += "out->%s_list = &%s_arena[g_num_%s];\n" % (el.name, el.type.name, el.type.name)
	out += "for(pugi::xml_node node = root.first_child(); node; node = node.next_sibling()){\n"
	out += "\tgtok_%s in = glex_%s(node.name());\n" % (t.cpp_type, t.cpp_type)

	out += "\tswitch(in){\n";
	for el in t.child_elements:
		out += "\tcase gtok_%s::%s:\n" % (t.cpp_type, to_token(el.name))
		out += indent(_gen_load_element(el, "out"), 2)
		out += "\t\tbreak;\n"
	out += "\tdefault: break; /* Not possible. */\n"
	out += "\t}\n";

	out += "}\n"
	return out

def _gen_load_attrs(t: XsdGroup) -> str:
	out = ""
	N = len(t.attribute_list)
	out += "std::bitset<%d> astate = 0;\n" % N
	out += "for(pugi::xml_attribute attr = root.first_attribute(); attr; attr = attr.next_attribute()){\n"
	out += "\tatok_%s in = alex_%s(attr.name());\n" % (t.cpp_type, t.cpp_type)
	out += "\tif(astate[(int)in] == 0) astate[(int)in] = 1;\n"
	out += "\telse throw std::runtime_error(\"Duplicate attribute \" + std::string(attr.name()) + \" in <%s>.\");\n" % t.name

	out += "\tswitch(in){\n";
	for attr in t.attribute_list:
		out += "\tcase atok_%s::%s:\n" % (t.cpp_type, to_token(attr.name))
		out += indent(_gen_load_simple(attr.type, "out->%s" % checked(attr.name), "attr.value()"), 2)
		out += "\t\tbreak;\n"
	out += "\tdefault: break; /* Not possible. */\n"
	out += "\t}\n";
	out += "}\n"

	mask = "".join(["1" if x.use == "optional" else "0" for x in t.attribute_list][::-1])
	out += "std::bitset<%d> test_state = astate | std::bitset<%d>(0b%s);\n" % (N, N, mask)
	out += "if(!test_state.all()) attr_error(test_state, atok_lookup_%s);\n" % t.cpp_type
	return out

def load_fn_from_complex_type(t: XsdComplexType) -> str:
	out = ""
	out += "void load_%s(const pugi::xml_node &root, %s *out){\n" % (t.name, t.cpp_type)
	if t.model:
		out += indent(_gen_load_group(t))
	elif t.has_simple_content():
		out += indent(_gen_load_simple(t.content_type, "out->value", "root.child_value()"))

	if t.attributes:
		out += indent(_gen_load_attrs(t))
	else:
		out += "\tif(root.first_attribute()) throw std::runtime_error(\"Unexpected attribute in <%s>.\");\n" % t.name

	out += "}\n"
	return out

load_fn_definitions = ""
for t in types:
	load_fn_definitions += load_fn_from_complex_type(t) + "\n"

#

def gen_alloc_arenas() -> str:
	out = ""
	out += "void alloc_arenas(void){\n"
	for t in sorted(arena_types, key=lambda x: x.name):
		out += "\tif(!(%s_arena = (%s *)std::calloc(g_num_%s, sizeof(%s))))\n" % (t.name, t.cpp_type, t.name, t.cpp_type)
		out += "\t\tthrow std::runtime_error(\"Couldn't get memory for <%s> arena.\");\n" % t.name
	for t in sorted(arena_types, key=lambda x: x.name):
		out += "\tg_num_%s = 0;\n" % t.name
	out += "}\n"
	return out

# TODO: I think there can be multiple root elements of the same kind.
# TODO: Add support for root elements which can appear more than once.
def gen_init_fn() -> str:
	out = ""
	out += "void get_root_element(const char *filename){\n"
	out += "\tpugi::xml_document doc;\n"
	out += "\tpugi::xml_parse_result result = doc.load_file(filename);\n"
	out += "\tif(!result)\n"
	out += "\t\tthrow std::runtime_error(\"Could not load XML file \" + std::string(filename) + \".\");\n"
	out += "\tfor(pugi::xml_node node= doc.first_child(); node; node = node.next_sibling()){\n"

	for i, el in enumerate(elements):
		out += "\t\t%s(std::strcmp(node.name(), \"%s\") == 0){\n" % ("if" if i == 0 else "else if", el.name)
		out += "\t\t\tcount_%s(node);\n" % el.name
		out += "\t\t\talloc_arenas();\n"
		out += indent(_gen_load_element(el), 3)
		out += "\t\t}\n"
	out += "\t\telse throw std::runtime_error(\"Invalid root-level element \" + std::string(node.name()));\n"

	out += "\t}\n"
	out += "}\n"
	return out

#

# We use a parameter to emit squashed attribute writes
# ex. `os << "index=\"" << y_list.index << "\"";`
# so that we don't generate 1e14 lines of code.
def _gen_write_simple(t: XsdSimpleType, container: str, attr_name: str="") -> str:
	out = ""
	if isinstance(t, XsdAtomicBuiltin) or isinstance(t, XsdList):
		if attr_name:
			out += "os << \" %s=\\\"\" << %s << \"\\\"\";\n" % (attr_name, container)
		else:
			out += "os << %s;\n" % container
	elif isinstance(t, XsdAtomicRestriction):
		if attr_name:
			out += "os << \" %s=\\\"\" << lookup_%s[(int)%s] << \"\\\"\";\n" % (attr_name, t.name, container)
		else:
			out += "os << lookup_%s[(int)%s];\n" % (t.name, container)
	elif isinstance(t, XsdUnion):
		for m in t.member_types:
			out += "if(%s.tag == type_tag::%s)" % (container, to_token(m.cpp_type))
			out += indent(_gen_write_simple(t, container + "." + to_union_member_type(m.cpp_type), attr_name))
	else:
		raise NotImplementedError("I don't know how to write out %s." % t)
	return out

def _gen_write_attr(a: XsdAttribute, container: str) -> str:
	out = ""
	new_container = "%s.%s" % (container, a.name)
	if a.use == "required" or a.default:
		out += _gen_write_simple(a.type, new_container, a.name)
	else:
		out += "if((bool)%s)\n" % new_container
		out += indent(_gen_write_simple(a.type, new_container, a.name))
	return out

# TODO: We always emit attributes with nonzero default values for now.
# If not, we would have to check against the nonzero value, which creates
# another case split for strings, enums, unions...
def _gen_write_element(el: XsdElement, container: str, level: int=0) -> str:
	out = ""
	i = chr(ord('i')+level)
	if el.type.attribute_list:
		out += "os << \"<%s\";\n" % el.name
		for a in el.type.attribute_list:
			out += _gen_write_attr(a, container)
		out += "os << \">\";\n"
	else:
		out += "os << \"<%s>\";\n" % el.name

	for e in el.type.child_elements:
		if e.many:
			out += "for(int %c=0; %c<%s.num_%s; %c++){\n" % (i, i, container, e.name, i)
			out += "\tauto &%s = %s.%s_list[%c];\n" % (checked(e.name), container, e.name, i)
			out += indent(_gen_write_element(e, checked(e.name), level+1))
			out += "}\n"
		else:
			new_container = "%s.%s" % (container, e.name)
			if e.optional:
				out += "if(%s.has_%s){\n" % (container, e.name)
				out += indent(_gen_write_element(e, new_container, level+1))
				out += "}\n"
			else:
				out += _gen_write_element(e, new_container, level+1)
	if el.type.has_simple_content():
		out += _gen_write_simple(el.type.content_type, el.name+".value")
	out += "os << \"</%s>\";\n" % el.name
	return out

def gen_write_fn(t: XsdElement) -> str:
	out = ""
	out += "void write_root_element(std::ostream &os){\n"
	out += "\t/* Print floating points with max double precision. */\n"
	out += "\tos.precision(std::numeric_limits<double>::max_digits10);\n"
	out += indent(_gen_write_element(t, t.name, 0))
	out += "}\n"
	return out

#

print("#include <bitset>")
print("#include <cstring>")
print("#include <iostream>")
print("#include <limits>")
print("#include <memory>")
print("#include <string>")
print("#include <vector>")
print("")
print("#include <error.h>")
print("#include <stddef.h>")
print("#include <stdint.h>")
print("#include \"pugixml.hpp\"")
print("")

print("/* All uxsdcxx functions and structs live in this namespace. */")
print("namespace %s {\n" % namespace)

print(triehash.gen_prelude())

if struct_declarations: print(struct_declarations)
if enum_tokens: print(enum_tokens)
if simple_types: print(type_tag_definition)
if union_definitions: print(union_definitions)
if struct_definitions: print(struct_definitions)

if arena_declarations: print(arena_declarations)
print(count_fn_declarations)
print(load_fn_declarations)
print(builtin_fn_declarations)

print("/**")
print(" * Tokens for attribute and node names.")
print("**/")
print(complex_type_tokens)

print("/**")
print(" * Lexing functions. These convert the const char *s of PugiXML to enums.")
print(" * You may find numerous \"break\"s there. Without them, a warning explosion ensues.")
print("**/")
if enum_lexers: print(enum_lexers)
print(complex_type_lexers)

if [x for x in types if x.model == "dfa"]: print(dfa_error_fn)
if [x for x in types if x.attributes]: print(attr_error_fn)
if [x for x in types if x.model == "all"]: print(all_error_fn)

print("/**")
print(" * Validating&counting functions. These do the tree validation and count elements to allocate arenas.")
print("**/")
print(count_fn_definitions)

print("/**")
print(" * This allocates space for the complex types which can occur more than once.")
print(" * It resets the global type counters for use with loading functions.")
print("**/")
print(gen_alloc_arenas())

print("/**")
print(" * Loading functions. These load the DOM data into the generated structures.")
print("**/")
print(load_fn_definitions, end="")

print("/**")
print(" * Global variable to hold the root element.")
print("**/")
print(element_definitions)

print("/**")
print(" * Load into root element from file.")
print("**/")
print(gen_init_fn())

print("/**")
print(" * Write to output stream from root element.")
print("**/")
print(gen_write_fn(elements[0]))

print("} /* namespace %s */\n" % namespace)
