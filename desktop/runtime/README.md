# Generated desktop runtime

`runtime-manifest.json` is deliberately marked incomplete in source control.
Run `python packaging/desktop/build_runtime.py --target macos-arm64` on Apple
Silicon macOS, or the matching `windows-x64` command on a native Windows x64
builder. The generated tree contains CPython, application sources, installed
dependencies, and Patchright's exact Chromium build. Recipients never run pip
or install Python.
