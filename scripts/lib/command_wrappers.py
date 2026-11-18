"""Options that take a value after a command wrapper."""

WRAPPER_ARGUMENTS = {
    "env": {"-u", "--unset"},
    "command": set[str](),
    "exec": {"-a"},
    "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "-T"},
    "nice": {"-n", "--adjustment"},
    "time": {"-f", "--format", "-o", "--output"},
}
