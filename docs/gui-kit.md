# Desktop GUIs (tier 4): notes, not a feature

**Nothing described here is built.** This repository has no GUI image, no GUI package kind, no recipe, no gate and no test for
operating a desktop program, and ToolForge does not claim it can. This page records what such a kit would need and why it is not
part of this release.

## What a `toolforge-runtime-gui` image would hold

A second runtime image, never an addition to `toolforge-runtime:py312`, so that ordinary tools do not carry its size or its risks:

| Part | Role | A common choice (not tried here) |
| --- | --- | --- |
| Virtual display | a screen with no hardware, of a fixed size | `Xvfb` |
| Window manager | predictable window placement | a minimal one, or none for a single maximised window |
| Screenshot tool | the current screen as an image | `scrot` or ImageMagick `import` |
| Input tool | mouse moves, clicks and key presses | `xdotool` |
| The target program | what the tool operates | chosen per tool, installed when the image is built |

It would run with the same hardening as the code sandbox (non-root, read-only root filesystem, no capabilities, no network unless
approved). Whether a given program starts under those settings, and within the memory and workspace limits of a tool, would have
to be measured program by program.

## Why it needs a vision step

A graphical program answers with pixels. Something has to turn a screenshot into facts ("the dialog is open", "the total is 42"),
and decide where to click. That takes a vision model or hand-made image matching. ToolForge has neither: its model client sends
text only, and MiniLocker has no vision step.

It would also weaken what verification means here:

* G3 and G4 compare exact JSON values. A reading of pixels by a model can differ between runs.
* Click positions depend on the screen size, fonts, theme, language and the program's version.
* Timing matters: the same click is right or wrong depending on whether a window has finished drawing.
* A tool that clicks by coordinates can succeed on every recorded screen and do something else on the next.

## What could be built without vision

Programs that expose their state as **text** need no screen at all, and most of them fit the tool kinds that already exist:

| The program has | Possible today as | Limits |
| --- | --- | --- |
| a command line or a text protocol (many engines and converters) | an ordinary `stateless` or `session` tool that starts the program and parses its output | the program must be in the runtime image or installable from a wheel; see [runtime-image.md](runtime-image.md) |
| files it reads and writes | a `stateless` tool working on the files | only text results are returned |
| a scripting interface or an embedded interpreter | a tool that writes a script and runs the program headless | the script language is another input to validate |
| a web interface | a [browser plan](browser-plans.md) | no login; public pages on approved hosts |
| an accessibility tree (for example AT-SPI) | **not built**: it needs the GUI image, and would give text state without a vision step | support differs a lot between programs |

A session tool keeps its `state` and its files between calls ([sessions.md](sessions.md)). It is not promised that a process it
started stays alive between calls, so a design should rebuild the program's state from `state`.

## Risks to settle before any of it exists

* **Prompt injection through pixels.** Text drawn on a screen cannot be cleaned or scanned the way ToolForge treats text output,
  and a vision model would read it as part of the picture.
* **No separation inside the display.** Under X11 any client of a display can read the others' windows and send them input, so the
  tool and the program it drives trust each other completely. The container stays the only boundary.
* **Actions with consequences.** A click can delete, send or buy. A permission model per action does not exist.
* **A larger attack surface.** A display server, a window manager and a whole application, none written for hostile input.
* **Resource cost.** A large image and a heavy process per call or per session.
* **Evidence.** Blast Radius and the gates measure files, processes and network. None of them says anything about what was done
  inside a window.
* **Licences.** Installing an application in an image is redistribution if the image is shared.

## Before anyone may claim it

A separate image and build command, its own package kind and recipe, gates that say what they can and cannot judge, tests that run
against a real container, a clear "experimental" label everywhere it appears, and measured results. Until then this page is the
only place ToolForge mentions how it might work.
