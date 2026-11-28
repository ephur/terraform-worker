# Hooks

Hooks are executables in a definition's `hooks` directory that run before and after each terraform action. Use them for work terraform does not do, such as building an image before a plan or registering a service after an apply.

```
definitions/new-ami
├── main.tf
├── outputs.tf
└── hooks
    ├── pre_plan.sh
    └── post_apply.py
```

## Naming

A hook is named `<stage>_<action>`, with any extension. The stage is `pre` or `post`, and the action is `init`, `plan`, `apply` or `destroy`, so `pre_init`, `post_plan` and `post_destroy` are all valid. The file must be executable; a matching file that is not executable fails the definition. The worker runs it directly, so its shebang line picks the interpreter, for example `#!/usr/bin/env python3`.

The hook runs with the `hooks` directory as its working directory and gets the stage and action as its two arguments, for example `pre_plan.sh pre plan`.

## Environment

The hook inherits the environment terraform runs with, including the backend credentials, plus:

| Variable | Value |
| --- | --- |
| `TF_PATH` | The terraform binary the worker uses. |
| `TF_VAR_<NAME>` | Each of the definition's `terraform_vars`. |
| `TF_REMOTE_<NAME>` | Each of the definition's `remote_vars`, resolved from the referenced state. |
| `TF_EXTRA_<NAME>` | Each of the definition's `template_vars`, merged with the global ones. |

Names are upper-cased, with `-` and `.` turned into `_`. Booleans are `TRUE` or `FALSE`, lists and maps are compact JSON, and every value is shell-quoted. With `--b64-encode`, values are base64 encoded instead (lists and maps as JSON first), which is easier to decode reliably. The worker warns when a value comes close to the shell's environment size limit.

Resolving `TF_REMOTE_*` reads every referenced state from the backend. A definition that does not need them in its hooks can set `hooks_disable_remotes: true` to skip that. Remote vars that reference a nested key (`network.outputs.subnets.private`, `network.outputs.subnets["private"][0]`) get the value at that key, the same value terraform sees; a key that does not exist in the state fails the hook.

## Failures

A hook that exits non-zero fails its definition in the phase the hook belongs to, exactly like a terraform failure: the phase's failure options decide whether the run continues (see [Running the worker](running.md#when-a-definition-fails)), and no later phase runs for that definition. Hook output is logged at debug level, and in full when the hook fails.
