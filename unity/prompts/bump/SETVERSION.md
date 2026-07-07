You are the primary agent running the **Set-Version** step of `unity bump`.

Set the project to the **target version specified in `.unity/UNITY.md`**, on the main branch. Do the
mechanical change only — do **not** fix any declarations. The project will build with errors afterward;
that is expected and fine (the bumping phase fixes the breakage).

Steps:
1. Read the target version from `.unity/UNITY.md` (a Lean toolchain and/or Mathlib version).
2. Update `lean-toolchain` to the target toolchain.
3. Update the Mathlib (and any other) dependency version in the `lakefile` (`lakefile.toml` or
   `lakefile.lean`) to the target.
4. Remove the `.lake` directory, then run `lake update` and `lake exe cache get`.
5. Commit on the main branch as `UNITY: bump to <target version>` and post a note on the forum with the
   exact from→to versions.

If the target version in `.unity/UNITY.md` is ambiguous or missing, say so on the forum and record the
question in `.unity/UNITY.md` rather than guessing a version.

**Norms:** operate only within the launch directory (the Lean project and `.unity/`). Do not edit any
Lean declarations in this step. Leave `.unity/critic.json` untouched. Consult the global unity library
(`~/.unity/library/`) for prior bump notes.
