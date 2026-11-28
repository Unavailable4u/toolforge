# Known limitations

What ToolForge does not do, or does less well than its description might suggest. Each line is one limitation; the project website
reads this file, so the format is fixed (a test enforces it).

* **One egress allowlist for every tool.** The egress proxy has a single allowlist and a single credential broker shared by all tools, and the allowlist only grows while a process runs. A tool approved for one host can therefore reach any host another approved tool needed, until the proxy restarts (`docs/upstream/minilocker-per-sandbox-allowlist.md`).
* **The challenger can be wrong.** Gate G5 is a second language model; it can miss a defect, invent a failing case, or be unavailable, in which case the gate is skipped and the tool stops at `verified_basic`. How often it finds a real defect has not been measured.
* **Static checks are advisory.** Gate G1 reads the code and is easy to get around with obfuscation; it is an early warning that saves a sandbox run, not a security boundary. The container is the boundary.
* **The container is not a virtual machine.** Tool code runs in hardened Docker containers that share the host kernel, so a kernel or runtime escape would defeat every gate. The attack lab shows what the catalogue of twenty-seven attacks did here, not what an unknown attack would do.
* **Containment evidence is a sample.** Gate G6 and the Blast Radius report say what was observed in the runs they measured, with the network closed and decoys planted. A clean report is evidence, not proof, and a dimension that was not measured says "not measured".
* **Python only.** Tools are Python 3.12 packages with pinned, wheel-only libraries from the runtime image. There is no other language runtime, and a library that is not in the image cannot be used by a tool.
* **No pixel-level control of a graphical program.** Browser plans drive a page through a closed vocabulary of steps against a fixed list of hosts; there is no screenshot-and-click loop. The GUI kit page is documentation only.
* **A cold start on every call.** A stateless tool runs in a fresh sandbox each call, which costs container start time. Session tools keep one sandbox alive between calls, with a time limit and a cap on how many are open.
* **One operator.** Promotion, approval, release and rollback are done by whoever runs the command line or opens the dashboard, with a shared bearer token on the network listeners. There are no accounts, roles or per-person audit.
* **The ledger is a hash chain, not a witness.** It shows that events were not edited after the fact; it cannot show that a consistent history really ran. A signed bundle from another operator is verified for its signature and its chain, not trusted for what it claims.
* **Injection detection is a heuristic.** Text from tools is fenced and marked as data, and text that looks like an instruction is flagged. A model can still be persuaded by text that no pattern recognises.
* **Results depend on the model and on your run.** The repository holds no benchmark results: the success rates are whatever `python -m bench` measures for your model and your machine. The scripted demo does not use a language model, so it says nothing about how often a real one writes a passing tool.
* **The tour shows one recorded run.** The committed recording was made with scripted agent replies in a stand-in sandbox with no isolation: G5 and G6 are skipped in it and the hostile tool is rejected before any decoy is read. The tour says so, and shows more only after a recording made with Docker replaces it.
* **Windows needs WSL2.** File modes, the harness and the sandbox assume Linux. Behaviour on Windows outside WSL2 has not been measured.
