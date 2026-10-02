/-
Bump-only Lake module discovery. Compile once per helper/toolchain revision:

  lake env lean -R <this file's directory> -c <cache>/workspace.c <this file>
  lake env leanc -o <cache>/workspace <cache>/workspace.c -lLake -rdynamic

Then run from the package root:

  lake env <cache>/workspace src/Example.lean src/Example/Defs.lean Main.lean

Native linking and interpreter symbol export (`-rdynamic` on Unix) are required
to evaluate arbitrary lakefile.lean configuration. The cached-configuration Lake
loader cannot safely run through `lean --run`; TOML alone does not expose this.

The final stdout line is JSON with `modules` (project-relative source path to
import name), `traces` (module name to package-relative trace file), `build_dir`,
`source_roots`, `unmatched`, and `issues`, plus `libraries` (Lake target names)
and `module_owners` (source path to library/executable target name arrays).
Pass --imports followed by checked paths to parse only their Lean headers;
this mode neither loads the Lake configuration nor elaborates any source.
Pass the controller's
already checked source paths; unowned scratch files are reported as unmatched.
The caller must reject an unmatched required target, and must still check source
containment/symlinks. Pass --layout-only to inspect configuration without source
enumeration. With no arguments, Lake's configured library globs and
executable roots are enumerated (not the transitive imports of those modules).

Use Lake's actual Lean/TOML package loader and source lookup, not a TOML parser
or path-to-namespace heuristic. Root-only loading deliberately avoids dependency
updates/materialization: loadWorkspace invokes the same package loader but may
update the manifest when it is missing. No build is performed by this helper.
-/
import Lake
import Lake.DSL
import Lake.Load.Package
import Lean.Elab.Import

open Lean Lake System

namespace UnityBumpWorkspace

private def checkedSource (root : FilePath) (filename : String) : IO FilePath := do
  let relative : FilePath := filename
  if relative.isAbsolute || relative.components.contains ".." || relative.extension != some "lean" then
    throw <| IO.userError s!"invalid project source path: {filename}"
  let path := (root / relative).normalize
  if (← IO.FS.realPath path) != path then
    throw <| IO.userError s!"project source is not canonical (symlink or alias): {filename}"
  return path

private def readImports (paths : List String) : IO Json := do
  let root ← IO.FS.realPath (← IO.currentDir)
  let mut imports : List (String × Json) := []
  let mut issues : Array String := #[]
  for filename in paths.eraseDups do
    try
      let path ← checkedSource root filename
      let contents ← IO.FS.readFile path
      let (header, state, messages) ← Lean.Parser.parseHeader (Lean.Parser.mkInputContext contents filename)
      if messages.hasErrors || state.recovering then
        let mut details : Array String := #[]
        for message in messages.toList do
          details := details.push (← message.toString)
        issues := issues.push s!"invalid Lean import header in {filename}: {String.intercalate "; " details.toList}"
        continue
      -- HeaderSyntax.imports is Lean's own implicit-Init/prelude and modern
      -- public/meta/import-all interpretation. Deduplicate only module names.
      let names := (Lean.Elab.HeaderSyntax.imports header).toList.map (·.module.toString)
      imports := ((FilePath.mk filename).normalize.toString, toJson names.eraseDups) :: imports
    catch error =>
      issues := issues.push s!"could not read Lean import header {filename}: {error}"
  return Json.mkObj [("imports", Json.mkObj imports), ("issues", toJson issues)]

private def loadRoot : IO Package := do
  let (elan?, lean?, lake?) ← findInstall?
  let some lean := lean? | throw <| IO.userError "could not locate Lean installation"
  let some lake := lake? | throw <| IO.userError "could not locate Lake installation"
  let lakeEnv ← match ← (Lake.Env.compute lake lean elan? (some true)).toBaseIO with
    | .ok env => pure env
    | .error message => throw <| IO.userError message
  let root ← IO.FS.realPath (← IO.currentDir)
  let some pkg ← (loadPackage {
    lakeEnv, wsDir := root, updateToolchain := false
  }).toBaseIO
    | throw <| IO.userError "could not load the root Lake package configuration"
  return pkg

private def discover (paths : List String) : IO Json := do
  let pkg ← loadRoot
  let layoutOnly := paths == ["--layout-only"]
  let mut paths := if layoutOnly then [] else paths
  let mut roots : Array Json := #[]
  let mut defaultModules : List (String × Json) := []
  let mut unknownDefaults : Array String := #[]
  for target in pkg.defaultTargets do
    if let some lib := pkg.findLeanLib? target then
      for mod in ← lib.getModuleArray do
        defaultModules := (mod.relLeanFile.normalize.toString, Json.str mod.name.toString) :: defaultModules
    else if let some exe := pkg.findLeanExe? target then
      defaultModules := (exe.root.relLeanFile.normalize.toString, Json.str exe.root.name.toString) :: defaultModules
    else
      unknownDefaults := unknownDefaults.push target.toString
  for lib in pkg.leanLibs do
    roots := roots.push <| Json.mkObj [
      ("kind", Json.str "library"), ("name", Json.str lib.name.toString),
      ("path", Json.str (relPathFrom pkg.dir lib.srcDir).normalize.toString)]
  for exe in pkg.leanExes do
    roots := roots.push <| Json.mkObj [
      ("kind", Json.str "executable"), ("name", Json.str exe.name.toString),
      ("path", Json.str (relPathFrom pkg.dir exe.root.rootDir).normalize.toString)]
  if paths.isEmpty && !layoutOnly then
    for lib in pkg.leanLibs do
      for mod in ← lib.getModuleArray do
        paths := paths ++ [mod.relLeanFile.normalize.toString]
    for exe in pkg.leanExes do
      paths := paths ++ [exe.root.relLeanFile.normalize.toString]
  let mut modules : List (String × Json) := []
  let mut owners : List (String × Json) := []
  let mut traces : List (String × Json) := []
  let mut unmatched : Array String := #[]
  let mut issues : Array String := #[]
  let mut seen : NameMap String := {}
  for filename in paths.eraseDups do
    let relative : FilePath := filename
    if relative.isAbsolute || relative.components.contains ".." || relative.extension != some "lean" then
      issues := issues.push s!"invalid project source path: {filename}"
      continue
    let path ← checkedSource pkg.dir filename
    let mut candidates : Array Lake.Module := #[]
    let mut libraryOwners : Array String := #[]
    let mut executableOwners : Array String := #[]
    for lib in pkg.leanLibs do
      if let some mod := lib.findModuleBySrc? path then
        if mod.leanFile.normalize == path then
          candidates := candidates.push mod
          libraryOwners := libraryOwners.push lib.name.toString
    for exe in pkg.leanExes do
      if let some mod := exe.isRootSrc? path then
        candidates := candidates.push mod
        executableOwners := executableOwners.push exe.name.toString
    let some mod := candidates[0]? | do
      unmatched := unmatched.push filename
      continue
    if candidates.any (·.name != mod.name) then
      issues := issues.push s!"ambiguous module ownership for source {filename}"
      continue
    -- Prefix-based source lookup alone can accept similarly named directories.
    -- Also require the canonical module target to resolve to these exact bytes.
    let some canonical := pkg.findTargetModule? mod.name | do
      issues := issues.push s!"source {filename} has no buildable module target"
      continue
    if mod.leanFile.normalize != path || canonical.leanFile.normalize != path then
      issues := issues.push s!"source {filename} conflicts with module {mod.name}"
      continue
    -- Lake resolves overlapping library declarations by their order. That must
    -- not hide another owner of the same module name at a different path.
    let conflictingLib := pkg.leanLibs.any fun lib =>
      (lib.findModule? mod.name).any (·.leanFile.normalize != path)
    let conflictingExe := pkg.leanExes.any fun exe =>
      (exe.isRoot? mod.name).any (·.leanFile.normalize != path)
    if conflictingLib || conflictingExe then
      issues := issues.push s!"ambiguous canonical ownership for module {mod.name} at {filename}"
      continue
    if let some previous := seen.find? mod.name then
      if previous != relative.normalize.toString then
        issues := issues.push s!"module {mod.name} maps to both {previous} and {filename}"
        continue
    seen := seen.insert mod.name relative.normalize.toString
    modules := (relative.normalize.toString, Json.str mod.name.toString) :: modules
    owners := (relative.normalize.toString, Json.mkObj [
      ("libraries", toJson (libraryOwners.qsort (· < ·))),
      ("executables", toJson (executableOwners.qsort (· < ·)))]) :: owners
    traces := (mod.name.toString,
      Json.str (relPathFrom pkg.dir mod.traceFile).normalize.toString) :: traces
  return Json.mkObj [
    ("modules", Json.mkObj modules), ("source_roots", Json.arr roots),
    ("module_owners", Json.mkObj owners),
    ("default_modules", Json.mkObj defaultModules),
    ("unknown_default_targets", toJson unknownDefaults),
    ("libraries", toJson ((pkg.leanLibs.map (·.name.toString)).qsort (· < ·))),
    ("traces", Json.mkObj traces),
    ("build_dir", Json.str (relPathFrom pkg.dir pkg.buildDir).normalize.toString),
    ("unmatched", toJson unmatched), ("issues", toJson issues)]

end UnityBumpWorkspace

def main (args : List String) : IO UInt32 := do
  try
    let result ← match args with
      | "--imports" :: paths => UnityBumpWorkspace.readImports paths
      | _ => UnityBumpWorkspace.discover args
    IO.println result.compress
    return if (result.getObjValAs? (Array String) "issues").toOption == some #[] then 0 else 1
  catch error =>
    IO.println (Json.mkObj [("modules", Json.mkObj []), ("source_roots", Json.arr #[]),
      ("module_owners", Json.mkObj []), ("libraries", Json.arr #[]),
      ("unmatched", Json.arr #[]), ("issues", toJson [s!"Lake workspace discovery failed: {error}"])]).compress
    return 1
