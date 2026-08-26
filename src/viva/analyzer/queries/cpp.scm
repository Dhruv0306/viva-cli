(function_definition
  declarator: (function_declarator
    declarator: (identifier) @name.function)) @definition.function

(function_definition
  declarator: (function_declarator
    declarator: (field_identifier) @name.function)) @definition.function

(class_specifier
  name: (type_identifier) @name.class) @definition.class
