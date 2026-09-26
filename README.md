<img src="assets/labrys.svg" width="72" height="72" alt="Labrys double axe">

# Labrys

A personal terminal tool I made for the Kubeflow notebooks I use at work.

It allows me to start a notebook, get a shell or a VS Code window, and stop it
when I'm done. It also connects to my 1Password and automatically handles
credentials. Basically, it keeps everything in one place so I don't have to
juggle kubeconfigs and SSH connections all the time.

If your setup looks similar, it might be useful.

Run it with `lab`, or `labrys` if you prefer the full name. They are the same command.

<div align="center">
  <img src="assets/demo.gif" alt="A demo video" width="1008" />
</div>

## Trying it

You'll need [uv](https://docs.astral.sh/uv/), the 1Password desktop app and a
read-only Service Account with access to your lab vault. Labrys supports Python
3.12 and 3.13. From this checkout:

```sh
uv tool install --python 3.12 .
lab init
```

Put a `<cluster-name>.yaml` in the profile directory you picked during init.
There's a [template](profiles/template.yaml) and an
[Agent Skill](.agents/skills/lab-profile/SKILL.md) to help with that.

```sh
lab cluster add
lab
```

The prompts walk through the 1Password steps; you still save the items in the
app. Quitting lab leaves the notebook running. Use Stop when you're done with it.

Shell access needs `kubectl` on your `PATH`. VS Code access also needs `code`;
see the [manual](docs/manual.md#setup) for accepted editor versions. SSH routes
use your existing SSH host configuration.

After updating the checkout, run `uv tool install --python 3.12 --force .` to
replace the installed version.

## Documentation

Run `man lab` for setup, commands, examples and troubleshooting. `man labrys`
opens the same manual. Use `lab notebook COMMAND --help` for a quick parameter
reference.

`uv tool install` includes the man pages in the tool environment. To let `man`
find them, add this once to your shell configuration:

```sh
export MANPATH="$(uv tool dir)/labrys/share/man:${MANPATH:-}"
```

The empty final entry preserves the system manual directories. The pages update
with the package. uv does not yet expose them automatically; see
[uv's man-page support issue](https://github.com/astral-sh/uv/issues/4731).

[Manual](docs/manual.md) · [Configuration](docs/configuration.md) ·
[Architecture and development](docs/architecture.md)
