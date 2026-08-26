"""One-command per-user installer for the OrthoSWIFT Metashape adapter."""
from __future__ import annotations
import argparse, json, os, platform, shutil, subprocess, sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
REQUIREMENTS = (
    (HERE / 'requirements-metashape.txt') if (HERE / 'requirements-metashape.txt').is_file()
    else ((HERE / 'requirements.txt') if (HERE / 'requirements.txt').is_file()
    else (HERE.parent / 'requirements.txt'))
)
CONFIG=HERE/'orthoswift_config.json'
ENTRY=HERE/'metashape.py'

def target_script_dirs() -> list[Path]:
    home = Path.home()
    system = platform.system()
    dirs = []
    if system == 'Windows':
        local = Path(os.environ.get('LOCALAPPDATA', home / 'AppData' / 'Local'))
        roaming = Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming'))
        for base in [local, roaming]:
            dirs.append(base / 'Agisoft' / 'Metashape Pro' / 'scripts')
    elif system == 'Darwin':
        dirs.append(home / 'Library' / 'Application Support' / 'Agisoft' / 'Metashape Pro' / 'scripts')
    else:
        dirs.append(home / '.local' / 'share' / 'Agisoft' / 'Metashape Pro' / 'scripts')
    return dirs

def install(python: str|None=None, skip_dependencies: bool=False) -> dict:
    if not ENTRY.is_file() or not (HERE / 'runner.py').is_file():
        raise RuntimeError('Incomplete installation bundle. Ensure all plugin files are extracted.')
    if not skip_dependencies and not REQUIREMENTS.is_file():
        raise RuntimeError(f'Dependency manifest not found: {REQUIREMENTS}')
    venv=HERE/'.venv'
    source_python=python or sys.executable
    probe=subprocess.run([source_python,'-c','import sys,json; print(json.dumps(list(sys.version_info[:2])))'],check=True,capture_output=True,text=True)
    version=tuple(json.loads(probe.stdout.strip()))
    if version < (3,10) or version > (3,12):
        raise RuntimeError(f'OrthoSWIFT requires Python 3.10-3.12; selected {version[0]}.{version[1]}')
    if not skip_dependencies:
        subprocess.run([source_python,'-m','venv',str(venv)],check=True)
        venv_python=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        subprocess.run([str(venv_python),'-m','pip','install','--upgrade','pip'],check=True)
        subprocess.run([str(venv_python),'-m','pip','install','-r',str(REQUIREMENTS)],check=True)
    else:
        venv_python=Path(source_python).resolve()
    CONFIG.write_text(json.dumps({'python':str(venv_python)},indent=2)+'\n',encoding='utf-8')
    
    loader_code = (
        "# OrthoSWIFT Metashape Startup Loader\n"
        "import sys, os, runpy, traceback\n\n"
        f"_entry = {repr(str(ENTRY))}\n"
        "try:\n"
        "    _plugin_dir = os.path.dirname(_entry)\n"
        "    if _plugin_dir not in sys.path:\n"
        "        sys.path.insert(0, _plugin_dir)\n"
        "    runpy.run_path(_entry, run_name='orthoswift_metashape_plugin')\n"
        "except Exception as _exc:\n"
        "    print(f'[OrthoSWIFT Loader Error]: {_exc}')\n"
        "    traceback.print_exc()\n"
    )
    
    installed_loaders = []
    for target_dir in target_script_dirs():
        target_dir.mkdir(parents=True, exist_ok=True)
        loader = target_dir / 'orthoswift_loader.py'
        loader.write_text(loader_code, encoding='utf-8')
        installed_loaders.append(str(loader))
        
    return {'plugin':str(ENTRY),'python':str(venv_python),'loaders':installed_loaders}

def uninstall() -> None:
    for target_dir in target_script_dirs():
        (target_dir / 'orthoswift_loader.py').unlink(missing_ok=True)

def main(argv=None):
    parser=argparse.ArgumentParser(description='Install OrthoSWIFT for the current Metashape user')
    parser.add_argument('--python',help='Python 3.10-3.12 executable used to create the isolated environment')
    parser.add_argument('--skip-dependencies',action='store_true',help='Use the selected Python directly; advanced/offline use')
    parser.add_argument('--uninstall',action='store_true')
    args=parser.parse_args(argv)
    if args.uninstall:
        uninstall();print('Removed OrthoSWIFT Metashape startup loader.');return 0
    try:
        result=install(args.python,args.skip_dependencies)
    except (RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f'OrthoSWIFT installation failed: {exc}', file=sys.stderr)
        print('Check that Python 3.10-3.12 and its venv support are installed, then retry.', file=sys.stderr)
        return 1
    print(json.dumps(result,indent=2));print('Restart Metashape. OrthoSWIFT will appear in the menu.');return 0
if __name__=='__main__':raise SystemExit(main())
