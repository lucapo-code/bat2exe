# bat2exe

Convierte scripts de Windows (`.bat`, `.cmd`, `.ps1`, `.vbs`, `.js`, `.wsf`) o cualquier archivo (por ejemplo `.py`) en un `.exe` de Windows (x86 y/o x64), corriendo todo desde **Termux** (Android).

El `.exe` generado lleva el script empaquetado adentro: al abrirlo, lo extrae a una carpeta temporal, lo ejecuta, y borra la copia temporal al terminar.

## Por qué existe

Para automatizar scripts propios en otra PC sin depender de que esa PC tenga Python, PowerShell habilitado, o nada instalado: el resultado es un único `.exe` que corre solo.

## Requisitos (en Termux)

```bash
pkg install llvm-mingw-w64 llvm-mingw-w64-ucrt llvm-mingw-w64-tools
```

## Uso básico

```bash
python3 bat2exe.py mi_script.bat -o mi_programa.exe
```

## Opciones

| Opción | Qué hace |
|---|---|
| `--hidden` | Corre sin ninguna ventana visible (solo aparece en el Administrador de tareas) |
| `--nowait` | El `.exe` no espera a que termine el script (por defecto sí espera y devuelve su código de salida) |
| `--single` | Una sola instancia a la vez: si ya hay una corriendo, la nueva se cierra |
| `--admin` | Pide permisos de administrador (UAC) al abrirlo |
| `--passargs` | Reenvía al script los argumentos con los que se abra el `.exe` |
| `--args "..."` | Argumentos fijos para el script |
| `--workdir exe\|temp` | Carpeta de trabajo: donde está el `.exe` (por defecto) o la carpeta temporal |
| `--icon archivo.ico` | Icono del `.exe` |

## Ejemplo

```bash
python3 bat2exe.py backup.ps1 -o backup.exe --hidden --single
```

Genera `backup.exe`: corre `backup.ps1` sin ventana, y si ya está corriendo una copia, la segunda no se abre.

## Cómo funciona por dentro

El `.exe` es un pequeño programa en C (compilado con `llvm-mingw-w64` desde Termux) que lleva el script original pegado al final del archivo, con un pequeño "pie" (`footer`) que indica tamaño, tipo de script y las opciones elegidas. Al ejecutarse, lee su propio footer, extrae el script a una carpeta temporal y lo corre con el intérprete que corresponda (`cmd`, `powershell`, `wscript`, `python`, etc.).
