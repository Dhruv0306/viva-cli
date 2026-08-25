(function_declaration
  name: (identifier) @name.function) @definition.function

(class_declaration
  name: (type_identifier) @name.class) @definition.class

(method_definition
  name: (property_identifier) @name.function) @definition.function
