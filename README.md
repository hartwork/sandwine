# What is sandwine?

**sandwine** is a command-line tool to run Windows applications on GNU/Linux
that offers more isolation than raw [Wine](https://www.winehq.org/)
and more convenience than raw [bubblewrap](https://github.com/containers/bubblewrap).
It *uses* Wine and bubblewrap, it does not replace them.
**sandwine** is Software Libre written in Python 3, and
is licensed under the "GPL v3 or later" license.


# Installation

```console
# pip3 install sandwine
```


# Usage Examples


### Install Winamp 5.66: no networking, no X11, no sounds, no access to `~/*` files

```
# cd ~/Downloads/
# sha256sum -c <(echo 'ac70a0c8a2928c91400b9ac3774b331f1d700f3486bab674dbd09da6b31fe130  winamp566_full_en-us.exe')
# WINEDEBUG=-all sandwine --dotwine winamp/:rw ./winamp566_full_en-us.exe /S /D='C:\Program' 'Files' '(x86)\Winamp 5.66'
```

(The weird quoting in `/D='C:\Program' 'Files' '(x86)\Winamp 5.66'`
is [documented behavior](https://nsis.sourceforge.io/Which_command_line_parameters_can_be_used_to_configure_installers%3F)
for NSIS.)


### Run installed Winamp: with sound, with nested X11, no networking, no `~/*` file access

```console
# sandwine --pulseaudio --x11 --dotwine winamp/:rw --pass ~/Music/:ro --configure -- winamp
```

Argument `--configure` will bring up `winecfg` prior to Winamp so that you have a chance at
unchecking these two boxes:

- `Graphics`:
    - `Allow the window manage to *decorate* the windows`
    - `Allow the window manage to *control* the windows`

If Winamp crashes right after showing the main window, run it once more,
there is some Wine bug at work here.


### Run Geiss Screensaver: with sound, with host X11 (careful!), no networking, no `~/*` file access

```console
sandwine --host-x11-danger-danger --pulseaudio --retry -- ./geiss.scr /S
```

`--host-x11-danger-danger` make sandwine talk to the host X11 server, which would
[expose you to keyloggers](https://blog.invisiblethings.org/2011/04/23/linux-security-circus-on-gui-isolation.html)
so please re-visit your threat model before using `--host-x11-danger-danger`.

`--retry` is used to start programs a second time that consistently
crash from graphics issues in a fresh Wine environment
the first but not the second time.
Potentially a bug in Wine, needs more investigation.

PS: The Geiss Screensaver has its home at [geisswerks.com](https://www.geisswerks.com/geiss/).


### Run wget: with networking, no X11, no sound, no access to `~/*` files

```console
# sandwine --network --no-wine -- wget -S -O/dev/null https://blog.hartwork.org/
```

Argument `--no-wine` is mostly intended for debugging,
but is needed here to invoke non-Wine wget.


# Under the Hood

**sandwine** aims to protect against Windows applications that:

- read and leak personal files through/to the Internet
- read and leak keystrokes from other running applications
  ([related post](https://blog.invisiblethings.org/2011/04/23/linux-security-circus-on-gui-isolation.html))
- modify/destroy personal files
- modify/destroy system files

To achieve that, by default the launched application:

- Sees no files in ``${HOME}`` and/or `/home/` (unless you pass `--pass PATH:{ro,rw}` for a related directory).
- Does not have access to the internet (unless you pass ``--network``).
- Does not have access to your local X11 server.
  (unless you enabled some form of X11 integration, ideally nested X11).
- Does not have access to your sound card.

So what is shared with the application by default then?


## What is Exposed by Default?


### Files

| Path | Content |
| ---- | ------- |
| `/` | new tmpfs |
| `/bin` | read-only bind mount |
| `/dev` | new devtmpfs |
| `/dev/dri` | read-write bind mount with device access |
| `/etc` | read-only bind mount |
| `${HOME}` | new tmpfs |
| `${HOME}/.wine` | new tmpfs |
| `/lib` | read-only bind mount |
| `/lib32` | read-only bind mount |
| `/lib64` | read-only bind mount |
| `/proc` | new procfs |
| `/sys` | read-only bind mount |
| `/tmp` | new tmpfs |
| `/usr` | read-only bind mount |


### Environment Variables

- `${DISPLAY}`
- `${HOME}`
- `${PATH}` (with known-unavailable entries removed)
- `${TERM}`
- `${USER}`


**sandwine** features include:

- A focus on security, usability, transparency
- Support for nested X11 (X2Go nxagent (seamless), Xephyr, Xnest, Xvfb)
- Support for PulseAudio


# Threat Model and Known Limitations

- If your life depends on the sandbox, please consider using
  a virtual machine rather than sandwine, e.g. because your username
  is exposed to the running application and depending on your threat model,
  that may be too much already.
  Also sandwine has not seen any known external security audits, yet.
- sandwine relies on [bubblewrap](https://github.com/containers/bubblewrap)
  for its security, so it can only be as secure as bubblewrap.
- sandwine does not keep the application from using loads of RAM, CPU time and/or disk space.
  If your concerns include **denial of service**, you need protection beyond sandwine.
- sandwine relies on sane file permissions in the places that are shared read-only.
  If you have files in e.g. `/etc` that contain credentials but are readable by
  unprivileged users, sandwine will do nothing to block that read access.
- If the Windows application to be run expects a GNU/Linux environment and includes
  **Linux Kernel exploit** code, then that exploit is not likely to be stopped by sandwine.
- If you manually allow the sandboxed application to communicate with an unsandboxed application
  and the latter executes commands for the former, then the sandbox cannot prevent privilege
  escalation.  Think of a model like the Docker daemon where whoever can talk to the Docker
  daemon can become root. If you use sandwine with something like that, sandwine will have a problem.
- Start-up time below 200ms is not a goal.


# Reporting Vulnerabilities

If you think you found a vulnerability in sandwine,
please reach out [via e-mail](https://github.com/hartwork)
so we can have a closer look
and [coordinate disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure).

---
[Sebastian Pipping](https://github.com/hartwork), Berlin, 2023
