# Workspace Coding Standards

## Function and Method Invocations

- **Use Named Parameters**: Always use named/keyword parameters for all function, method, and constructor invocations instead of positional arguments (e.g., `calculate_metrics(data=records, threshold=0.5)` instead of `calculate_metrics(records, 0.5)`).
- **Preserve Function Signatures**: Do not alter function signatures or parameter definitions (such as adding keyword-only `*` separators) to enforce this. This is strictly an invocation-level preference.
- **Built-in Exceptions**: Standard Python built-in functions that disallow keyword arguments or are positional-only by definition (e.g., `len()`, `isinstance()`, `getattr()`, `setattr()`, `hasattr()`, `range()`, `enumerate()`, `zip()`) may be called with standard positional syntax.

## Database Calls

- **Use Named Binds**: Database calls that use bind variables must define the binds as a dictionary or named bind parameters instead of positional bind sequences.
