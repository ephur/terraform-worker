package main

import (
    "encoding/json"
    "flag"
    "fmt"
    "io"
    "os"

    convert "github.com/tmccombs/hcl2json/convert"
)

// This small CLI bridges HashiCorp's native Go HCL parser into tfworker.
// It reads HCL from a file or stdin and emits a JSON object to stdout that
// closely matches python-hcl2's structure to minimize Python-side changes.
//
// Build:
//   go build -o tfworker-hcl2json ./tools/hcl2json
// Usage:
//   tfworker-hcl2json path/to/file.hcl
//   cat file.hcl | tfworker-hcl2json --stdin
func main() {
    useStdin := flag.Bool("stdin", false, "read HCL from stdin")
    multi := flag.Bool("multi", false, "parse multiple files and emit {ok, errors}")
    flag.Parse()

    var (
        b   []byte
        err error
    )

    if *multi {
        if *useStdin {
            fmt.Fprintln(os.Stderr, "--multi does not support --stdin; pass file paths")
            os.Exit(2)
        }
        if flag.NArg() == 0 {
            fmt.Fprintln(os.Stderr, "--multi requires at least one file path")
            os.Exit(2)
        }
        ok := map[string]any{}
        errs := map[string]string{}
        for i := 0; i < flag.NArg(); i++ {
            fp := flag.Arg(i)
            b, err := os.ReadFile(fp)
            if err != nil {
                errs[fp] = err.Error()
                continue
            }
            jb, err := convert.Bytes(b, fp, convert.Options{})
            if err != nil {
                errs[fp] = err.Error()
                continue
            }
            var obj map[string]any
            if err := json.Unmarshal(jb, &obj); err != nil {
                errs[fp] = err.Error()
                continue
            }
            ok[fp] = obj
        }
        out := map[string]any{"ok": ok, "errors": errs}
        enc := json.NewEncoder(os.Stdout)
        enc.SetEscapeHTML(false)
        if err := enc.Encode(out); err != nil {
            fmt.Fprintln(os.Stderr, err)
            os.Exit(1)
        }
        os.Exit(0)
    }

    if *useStdin {
        b, err = io.ReadAll(os.Stdin)
        if err != nil {
            fmt.Fprintln(os.Stderr, err)
            os.Exit(1)
        }
    } else {
        if flag.NArg() != 1 {
            fmt.Fprintln(os.Stderr, "expected a single HCL file path or --stdin")
            os.Exit(2)
        }
        fp := flag.Arg(0)
        b, err = os.ReadFile(fp)
        if err != nil {
            fmt.Fprintln(os.Stderr, err)
            os.Exit(1)
        }
    }

    // Use convert.Bytes to parse and convert to JSON bytes
    jsonBytes, err := convert.Bytes(b, "<stdin>", convert.Options{})
    if err != nil {
        fmt.Fprintln(os.Stderr, err)
        os.Exit(1)
    }

    // Ensure we print normalized JSON (convert.Bytes already returns canonical JSON)
    var obj map[string]any
    if err := json.Unmarshal(jsonBytes, &obj); err != nil {
        fmt.Fprintln(os.Stderr, err)
        os.Exit(1)
    }

    enc := json.NewEncoder(os.Stdout)
    enc.SetEscapeHTML(false)
    if err := enc.Encode(obj); err != nil {
        fmt.Fprintln(os.Stderr, err)
        os.Exit(1)
    }
}
