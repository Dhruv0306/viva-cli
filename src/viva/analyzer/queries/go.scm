(function_declaration
  name: (identifier) @name.function) @definition.function

(method_declaration
  name: (field_identifier) @name.function) @definition.function

(type_declaration
  (type_spec
    name: (type_identifier) @name.class
    (struct_type))) @definition.class
