/-
Solve-only Lake module discovery. Compile once per helper/toolchain revision:

  lake env lean -R <this file's directory> -c <cache>/workspace.c <this file>
  lake env leanc -o <cache>/workspace <cache>/workspace.c -lLake -rdynamic

Then run from the package root:

  lake env <cache>/workspace src/Example.lean src/Example/Defs.lean Main.lean

Native linking and interpreter symbol export (`-rdynamic` on Unix) are required
to evaluate arbitrary lakefile.lean configuration. The cached-configuration Lake
loader cannot safely run through `lean --run`; TOML alone does not expose this.

The final stdout line is JSON with `modules` (project-relative source path to
import name), `traces` (module name to package-relative trace file), `build_dir`,
`source_roots`, `unmatched`, and `issues`. Pass the controller's
already checked source paths; unowned scratch files are reported as unmatched.
The caller must reject an unmatched required target, and must still check source
containment/symlinks. With no arguments, Lake's configured library globs and
executable roots are enumerated (not the transitive imports of those modules).

Use Lake's actual Lean/TOML package loader and source lookup, not a TOML parser
or path-to-namespace heuristic. Root-only loading deliberately avoids dependency
updates/materialization: loadWorkspace invokes the same package loader but may
update the manifest when it is missing. No build is performed by this helper.
-/
import Lake
import Lake.DSL
import Lake.Load.Package

open Lean Lake System

namespace UnitySolveWorkspace

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
  let mut paths := paths
  let mut roots : Array Json := #[]
  for lib in pkg.leanLibs do
    roots := roots.push <| Json.mkObj [
      ("kind", Json.str "library"), ("name", Json.str lib.name.toString),
      ("path", Json.str (relPathFrom pkg.dir lib.srcDir).normalize.toString)]
  for exe in pkg.leanExes do
    roots := roots.push <| Json.mkObj [
      ("kind", Json.str "executable"), ("name", Json.str exe.name.toString),
      ("path", Json.str (relPathFrom pkg.dir exe.root.rootDir).normalize.toString)]
  if paths.isEmpty then
    for lib in pkg.leanLibs do
      for mod in ← lib.getModuleArray do
        paths := paths ++ [mod.relLeanFile.normalize.toString]
    for exe in pkg.leanExes do
      paths := paths ++ [exe.root.relLeanFile.normalize.toString]
  let mut modules : List (String × Json) := []
  let mut traces : List (String × Json) := []
  let mut unmatched : Array String := #[]
  let mut issues : Array String := #[]
  let mut seen : NameMap String := {}
  for filename in paths.eraseDups do
    let relative : FilePath := filename
    if relative.isAbsolute || relative.components.contains ".." || relative.extension != some "lean" then
      issues := issues.push s!"invalid project source path: {filename}"
      continue
    let path := (pkg.dir / relative).normalize
    let some mod := pkg.findModuleBySrc? path | do
      unmatched := unmatched.push filename
      continue
    -- Prefix-based source lookup alone can accept similarly named directories.
    -- Also require the canonical module target to resolve to these exact bytes.
    let some canonical := pkg.findTargetModule? mod.name | do
      issues := issues.push s!"source {filename} has no buildable module target"
      continue
    if mod.leanFile.normalize != path || canonical.leanFile.normalize != path then
      issues := issues.push s!"source {filename} conflicts with module {mod.name}"
      continue
    if let some previous := seen.find? mod.name then
      if previous != relative.normalize.toString then
        issues := issues.push s!"module {mod.name} maps to both {previous} and {filename}"
        continue
    seen := seen.insert mod.name relative.normalize.toString
    modules := (relative.normalize.toString, Json.str mod.name.toString) :: modules
    traces := (mod.name.toString,
      Json.str (relPathFrom pkg.dir mod.traceFile).normalize.toString) :: traces
  return Json.mkObj [
    ("modules", Json.mkObj modules), ("source_roots", Json.arr roots),
    ("traces", Json.mkObj traces),
    ("build_dir", Json.str (relPathFrom pkg.dir pkg.buildDir).normalize.toString),
    ("unmatched", toJson unmatched), ("issues", toJson issues)]

end UnitySolveWorkspace

def main (args : List String) : IO UInt32 := do
  try
    let result ← UnitySolveWorkspace.discover args
    IO.println result.compress
    return if (result.getObjValAs? (Array String) "issues").toOption == some #[] then 0 else 1
  catch error =>
    IO.println (Json.mkObj [("modules", Json.mkObj []), ("source_roots", Json.arr #[]),
      ("unmatched", Json.arr #[]), ("issues", toJson [s!"Lake workspace discovery failed: {error}"])]).compress
    return 1
