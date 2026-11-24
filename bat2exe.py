#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bat2exe - convierte scripts (.bat .cmd .ps1 .vbs .js .wsf .py) o cualquier archivo en un .exe de Windows
(x86 y/o x64) desde Termux. El .exe lleva el script dentro; al abrirlo lo extrae a una carpeta
temporal, lo ejecuta y la borra al terminar.

Opciones principales:
  --hidden     sin ventana (solo aparece en el Administrador de tareas)
  --nowait     el .exe no espera a que termine el script (por defecto espera y devuelve su codigo de salida)
  --single     una sola instancia a la vez (si ya esta en marcha, el segundo se cierra)
  --admin      pide permisos de administrador (UAC) al abrirlo
  --passargs   reenvia al script los argumentos con los que se abra el .exe
  --args "..." argumentos fijos para el script
  --workdir exe|temp   carpeta de trabajo: donde esta el .exe (por defecto) o la carpeta temporal
  --icon x.ico icono del .exe
Requiere:  pkg install llvm-mingw-w64 llvm-mingw-w64-ucrt llvm-mingw-w64-tools
"""
import argparse, hashlib, os, shutil, struct, subprocess, sys, tempfile

MAGIC = b"B2EPAY01"
F_HIDDEN, F_NOWAIT, F_SINGLE, F_WORKTEMP, F_PASSARGS, F_INTEMP, F_CUSTOMPATH = 1, 2, 4, 8, 16, 32, 64
T_BAT, T_PS1, T_WSH, T_PY, T_OTHER = 0, 1, 2, 3, 4

STUB_C = r'''
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#pragma pack(push,1)
typedef struct { char magic[8]; uint32_t payload_len, flags, type, args_len, name_len, path_len; } Footer;
#pragma pack(pop)

#define F_HIDDEN 1
#define F_NOWAIT 2
#define F_SINGLE 4
#define F_WORKTEMP 8
#define F_PASSARGS 16
#define F_INTEMP 32
#define F_CUSTOMPATH 64
#define T_BAT 0
#define T_PS1 1
#define T_WSH 2
#define T_PY 3
#define T_OTHER 4

#ifdef GUI
#define FAIL(m) return 90
#else
#define FAIL(m) do { fprintf(stderr, "bat2exe: %s\n", m); return 90; } while (0)
#endif

static const char *skip_prog(const char *c) {
    if (*c == '"') { c++; while (*c && *c != '"') c++; if (*c) c++; }
    else while (*c && *c != ' ' && *c != '\t') c++;
    while (*c == ' ' || *c == '\t') c++;
    return c;
}

static int read_at(HANDLE f, LONGLONG off, void *buf, DWORD n) {
    LARGE_INTEGER li; DWORD got = 0;
    li.QuadPart = off;
    if (!SetFilePointerEx(f, li, NULL, FILE_BEGIN)) return 0;
    if (n == 0) return 1;
    return ReadFile(f, buf, n, &got, NULL) && got == n;
}

static int mkdirs(char *path) {
    char *p;
    for (p = path + 2; *p; p++) { /* saltar "C:" */
        if (*p == '\\') {
            *p = 0;
            CreateDirectoryA(path, NULL);
            *p = '\\';
        }
    }
    return CreateDirectoryA(path, NULL) || GetLastError() == ERROR_ALREADY_EXISTS;
}

static int run(void) {
    char self[MAX_PATH * 2], exedir[MAX_PATH * 2], tmpdir[MAX_PATH * 2], file[MAX_PATH * 3], mname[64];
    char *name, *fixed, *custompath, *payload, *cmd; const char *uargs; const char *workdir;
    int using_custom = 0, using_autotemp = 0;
    Footer ft; HANDLE f, out; LARGE_INTEGER sz; LONGLONG base; DWORD got, i;
    STARTUPINFOA si; PROCESS_INFORMATION pi; DWORD code = 0, cflags = 0;
    int hidden, wait;
    size_t clen;

    GetModuleFileNameA(NULL, self, sizeof self);
    f = CreateFileA(self, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) FAIL("no puedo abrir el ejecutable");
    if (!GetFileSizeEx(f, &sz) || sz.QuadPart < (LONGLONG)sizeof ft) FAIL("archivo invalido");
    if (!read_at(f, sz.QuadPart - (LONGLONG)sizeof ft, &ft, sizeof ft) || memcmp(ft.magic, "B2EPAY01", 8)) FAIL("no hay script incrustado");
    base = sz.QuadPart - (LONGLONG)sizeof ft - (LONGLONG)ft.name_len - ft.args_len - ft.path_len - ft.payload_len;
    if (base < 0) FAIL("datos danados");
    name = (char *)malloc(ft.name_len + 1);
    fixed = (char *)malloc(ft.args_len + 1);
    custompath = (char *)malloc(ft.path_len + 1);
    payload = (char *)malloc(ft.payload_len ? ft.payload_len : 1);
    if (!name || !fixed || !custompath || !payload) FAIL("sin memoria");
    if (!read_at(f, base, name, ft.name_len) ||
        !read_at(f, base + ft.name_len, fixed, ft.args_len) ||
        !read_at(f, base + ft.name_len + ft.args_len, custompath, ft.path_len) ||
        !read_at(f, base + ft.name_len + ft.args_len + ft.path_len, payload, ft.payload_len)) FAIL("no puedo leer el script");
    name[ft.name_len] = 0; fixed[ft.args_len] = 0; custompath[ft.path_len] = 0;
    CloseHandle(f);

    hidden = (ft.flags & F_HIDDEN) != 0;
    wait = (ft.flags & F_NOWAIT) == 0;

    if (ft.flags & F_SINGLE) {
        unsigned h = 5381; const char *p;
        HANDLE m;
        for (p = self; *p; p++) h = h * 33 + (unsigned char)*p;
        snprintf(mname, sizeof mname, "Local\\b2e_%08x", h);
        m = CreateMutexA(NULL, FALSE, mname);
        if (m && GetLastError() == ERROR_ALREADY_EXISTS) return 0;
    }

    strcpy(exedir, self);
    { char *s = strrchr(exedir, '\\'); if (s) *s = 0; }
    GetTempPathA(sizeof tmpdir - 64, tmpdir);
    snprintf(tmpdir + strlen(tmpdir), 64, "b2e_%08lx%08lx", (unsigned long)GetTickCount(), (unsigned long)GetCurrentProcessId());
    if (!CreateDirectoryA(tmpdir, NULL)) FAIL("no puedo crear la carpeta temporal");
    out = INVALID_HANDLE_VALUE;
    if (ft.flags & F_CUSTOMPATH) { /* ruta fija elegida al convertir (--ruta) */
        char dir[MAX_PATH * 2]; int mkr; DWORD mke, cfe;
        FILE *dbg = fopen("C:\\b2edebug.log", "a");
        snprintf(dir, sizeof dir, "%s", custompath);
        mkr = mkdirs(dir); mke = GetLastError();
        snprintf(file, sizeof file, "%s\\%s", dir, name);
        out = CreateFileA(file, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        cfe = GetLastError();
        if (dbg) { fprintf(dbg, "dir=[%s] mkdirs=%d mkerr=%lu file=[%s] out=%p cfe=%lu\n", dir, mkr, mke, file, (void*)out, cfe); fclose(dbg); }
        using_custom = 1;
    }
    if (out == INVALID_HANDLE_VALUE && !(ft.flags & F_INTEMP) && !(ft.flags & F_CUSTOMPATH)) {
        /* junto al .exe: asi %~dp0 y las rutas relativas del script apuntan a su carpeta */
        snprintf(file, sizeof file, "%s\\b2e_%08lx_%s", exedir, (unsigned long)GetTickCount(), name);
        out = CreateFileA(file, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_HIDDEN, NULL);
    }
    if (out == INVALID_HANDLE_VALUE) {
        snprintf(file, sizeof file, "%s\\%s", tmpdir, name);
        out = CreateFileA(file, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        using_autotemp = 1;
    }
    if (out == INVALID_HANDLE_VALUE) FAIL("no puedo escribir el script temporal");
    if (!WriteFile(out, payload, ft.payload_len, &got, NULL) || got != ft.payload_len) FAIL("escritura incompleta");
    CloseHandle(out);

    uargs = (ft.flags & F_PASSARGS) ? skip_prog(GetCommandLineA()) : "";
    if (ft.flags & F_CUSTOMPATH) workdir = (ft.flags & F_WORKTEMP) ? tmpdir : custompath;
    else workdir = (ft.flags & F_WORKTEMP) ? tmpdir : exedir;

    clen = strlen(file) + strlen(fixed) + strlen(uargs) + 512;
    cmd = (char *)malloc(clen);
    if (!cmd) FAIL("sin memoria");

    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = hidden ? SW_HIDE : SW_SHOWNORMAL;
    cflags = hidden ? CREATE_NO_WINDOW : 0;

    if (ft.type == T_OTHER) {
        SHELLEXECUTEINFOA se;
        ZeroMemory(&se, sizeof se);
        se.cbSize = sizeof se; se.fMask = SEE_MASK_NOCLOSEPROCESS; se.lpVerb = "open"; se.lpFile = file;
        se.lpParameters = (fixed[0] || uargs[0]) ? (snprintf(cmd, clen, "%s %s", fixed, uargs), cmd) : NULL;
        se.lpDirectory = workdir; se.nShow = hidden ? SW_HIDE : SW_SHOWNORMAL;
        if (!ShellExecuteExA(&se)) { code = 4; goto cleanup; }
        if (wait && se.hProcess) { WaitForSingleObject(se.hProcess, INFINITE); GetExitCodeProcess(se.hProcess, &code); }
        if (se.hProcess) CloseHandle(se.hProcess);
        goto cleanup;
    }

    switch (ft.type) {
    case T_BAT:
        snprintf(cmd, clen, "cmd.exe /d /c \"\"%s\" %s %s\"", file, fixed, uargs); break;
    case T_PS1:
        snprintf(cmd, clen, "powershell.exe -NoProfile -ExecutionPolicy Bypass %s-File \"%s\" %s %s", hidden ? "-WindowStyle Hidden " : "", file, fixed, uargs); break;
    case T_WSH:
        snprintf(cmd, clen, hidden ? "wscript.exe //B //Nologo \"%s\" %s %s" : "cscript.exe //Nologo \"%s\" %s %s", file, fixed, uargs); break;
    default: /* T_PY */
        snprintf(cmd, clen, "%s \"%s\" %s %s", hidden ? "pythonw.exe" : "python.exe", file, fixed, uargs); break;
    }

    ZeroMemory(&pi, sizeof pi);
    if (!CreateProcessA(NULL, cmd, NULL, NULL, TRUE, cflags, NULL, workdir, &si, &pi)) {
#ifndef GUI
        fprintf(stderr, "bat2exe: no pude ejecutar el interprete (error %lu)\n", GetLastError());
#endif
        code = 5; goto cleanup;
    }
    if (wait) { WaitForSingleObject(pi.hProcess, INFINITE); GetExitCodeProcess(pi.hProcess, &code); }
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);

cleanup:
    if (wait) {
        DeleteFileA(file);
        if (using_autotemp) {
            for (i = 0; i < 15; i++) {
                if (RemoveDirectoryA(tmpdir)) break;
                Sleep(200);
            }
        }
        (void)using_custom;
    }
    return (int)code;
}

#ifdef GUI
int WINAPI WinMain(HINSTANCE a, HINSTANCE b, LPSTR c, int d) { (void)a; (void)b; (void)c; (void)d; ExitProcess((UINT)run()); return 0; }
#else
int main(void) { return run(); }
#endif
'''

MANIFEST = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
    <requestedExecutionLevel level="%LEVEL%" uiAccess="false"/>
  </requestedPrivileges></security></trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application>
    <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
    <supportedOS Id="{1f676c76-80e1-4239-95bb-83d0f6d0da78}"/>
  </application></compatibility>
</assembly>
'''

CACHE = os.path.join(os.path.expanduser("~"), ".cache", "bat2exe")


def die(msg):
    print("bat2exe: " + msg, file=sys.stderr)
    sys.exit(1)


def tool(arch, name):
    prefix = "x86_64" if arch == "x64" else "i686"
    path = shutil.which("%s-w64-mingw32-%s" % (prefix, name))
    if not path:
        die("falta %s-w64-mingw32-%s. Instala:  pkg install llvm-mingw-w64 llvm-mingw-w64-ucrt llvm-mingw-w64-tools" % (prefix, name))
    return path


def build_stub(arch, gui, admin, icon):
    src_hash = hashlib.sha1((STUB_C + MANIFEST).encode()).hexdigest()[:8]
    icon_bytes = open(icon, "rb").read() if icon else b""
    key = "%s_%s_%s_%s%s" % (arch, "gui" if gui else "con", "adm" if admin else "usr", src_hash,
                             ("_" + hashlib.sha1(icon_bytes).hexdigest()[:8]) if icon else "")
    os.makedirs(CACHE, exist_ok=True)
    out = os.path.join(CACHE, key + ".exe")
    if os.path.exists(out):
        return out
    tmp = tempfile.mkdtemp(prefix="b2e_")
    try:
        open(os.path.join(tmp, "stub.c"), "w").write(STUB_C)
        open(os.path.join(tmp, "app.manifest"), "w").write(MANIFEST.replace("%LEVEL%", "requireAdministrator" if admin else "asInvoker"))
        rc = '1 24 "app.manifest"\n'
        if icon:
            open(os.path.join(tmp, "icon.ico"), "wb").write(icon_bytes)
            rc += '2 ICON "icon.ico"\n'
        open(os.path.join(tmp, "app.rc"), "w").write(rc)
        r = subprocess.run([tool(arch, "windres"), "-O", "coff", "app.rc", "res.o"], cwd=tmp, capture_output=True, text=True)
        if r.returncode:
            die("windres fallo:\n" + r.stderr)
        cmd = [tool(arch, "clang"), "-O2", "-s", "-static", "-mwindows" if gui else "-mconsole"]
        if gui:
            cmd.append("-DGUI")
        cmd += ["stub.c", "res.o", "-o", out + ".tmp", "-luser32", "-lshell32"]
        r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode:
            die("clang fallo:\n" + r.stderr)
        os.replace(out + ".tmp", out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def detect_type(path, forced):
    if forced:
        return {"bat": T_BAT, "ps1": T_PS1, "vbs": T_WSH, "js": T_WSH, "py": T_PY, "other": T_OTHER}[forced]
    ext = os.path.splitext(path)[1].lower()
    if ext in (".bat", ".cmd"):
        return T_BAT
    if ext == ".ps1":
        return T_PS1
    if ext in (".vbs", ".vbe", ".js", ".jse", ".wsf"):
        return T_WSH
    if ext == ".py":
        return T_PY
    return T_OTHER


def main():
    ap = argparse.ArgumentParser(description="Convierte scripts/archivos en .exe de Windows (x86/x64).", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Ejemplos:\n  bat2exe tarea.bat --hidden\n  bat2exe backup.ps1 --hidden --single -a x64 -o backup\n  bat2exe app.bat --admin --icon app.ico --passargs")
    ap.add_argument("archivo")
    ap.add_argument("-o", "--output", help="nombre de salida (sin o con .exe)")
    ap.add_argument("-a", "--arch", choices=["x86", "x64", "both"], default="both", help="x86 corre en 32 y 64 bits; x64 solo 64 bits (default: both)")
    ap.add_argument("--hidden", action="store_true", help="sin ventana: solo visible en el Administrador de tareas")
    ap.add_argument("--nowait", action="store_true", help="no esperar a que termine el script")
    ap.add_argument("--single", action="store_true", help="una sola instancia a la vez")
    ap.add_argument("--admin", action="store_true", help="pedir permisos de administrador")
    ap.add_argument("--passargs", action="store_true", help="pasar al script los argumentos del .exe")
    ap.add_argument("--args", default="", help="argumentos fijos para el script")
    ap.add_argument("--workdir", choices=["exe", "temp"], default="exe", help="carpeta de trabajo (default: la del .exe)")
    ap.add_argument("--extract", choices=["exe", "temp"], default="exe", help="donde se extrae el script: junto al .exe (default; %%~dp0 funciona) o en %%TEMP%%")
    ap.add_argument("--ruta", metavar="RUTA", help=r"ruta fija donde se extrae el script cada vez (ej. --ruta C:\\ProgramData\\Tareas), tiene prioridad sobre --extract")
    ap.add_argument("--icon", help="icono .ico")
    ap.add_argument("--type", choices=["bat", "ps1", "vbs", "js", "py", "other"], help="forzar tipo (por defecto por extension)")
    ap.add_argument("--dl", action="store_true", help="copiar tambien a /storage/emulated/0/Download")
    ap.add_argument("--clean", action="store_true", help="borrar la cache de stubs y salir")
    if "--clean" in sys.argv:
        shutil.rmtree(CACHE, ignore_errors=True)
        print("Cache borrada.")
        return
    a = ap.parse_args()

    if not os.path.isfile(a.archivo):
        die("no existe el archivo: " + a.archivo)
    if a.icon and not os.path.isfile(a.icon):
        die("no existe el icono: " + a.icon)
    payload = open(a.archivo, "rb").read()
    name = os.path.basename(a.archivo).encode("utf-8")
    fixed = a.args.encode("utf-8")
    typ = detect_type(a.archivo, a.type)
    flags = (F_HIDDEN if a.hidden else 0) | (F_NOWAIT if a.nowait else 0) | (F_SINGLE if a.single else 0) | \
            (F_WORKTEMP if a.workdir == "temp" else 0) | (F_PASSARGS if a.passargs else 0) | (F_INTEMP if a.extract == "temp" else 0)
    rutabytes = a.ruta.encode("utf-8") if a.ruta else b""
    if a.ruta:
        flags |= F_CUSTOMPATH
    footer = struct.pack("<8sIIIIII", MAGIC, len(payload), flags, typ, len(fixed), len(name), len(rutabytes))

    base = a.output or os.path.splitext(os.path.basename(a.archivo))[0]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    outdir = os.path.dirname(os.path.abspath(a.output)) if a.output and os.path.dirname(a.output) else os.path.dirname(os.path.abspath(a.archivo))
    base = os.path.basename(base)
    archs = ["x86", "x64"] if a.arch == "both" else [a.arch]

    made = []
    for arch in archs:
        stub = build_stub(arch, a.hidden, a.admin, a.icon)
        suffix = ("-" + arch) if len(archs) > 1 else ""
        dst = os.path.join(outdir, base + suffix + ".exe")
        with open(stub, "rb") as s, open(dst, "wb") as d:
            d.write(s.read()); d.write(name); d.write(fixed); d.write(rutabytes); d.write(payload); d.write(footer)
        os.chmod(dst, 0o755)
        made.append(dst)
        if a.dl and os.path.isdir("/storage/emulated/0/Download"):
            shutil.copy(dst, "/storage/emulated/0/Download/")
    modo = "oculto (sin ventana)" if a.hidden else "con ventana"
    print("Listo (%s, %s%s):" % (modo, "admin, " if a.admin else "", "espera al script" if not a.nowait else "no espera"))
    for m in made:
        print("  %s  (%d KB)" % (m, os.path.getsize(m) // 1024))


if __name__ == "__main__":
    main()
