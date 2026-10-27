"""A small local console menu for double-click Windows launchers."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    root = parser.parse_args().root.resolve()
    db = root / "data" / "state.sqlite3"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    while True:
        print("\nHRPres — javne hrvatske sudske odluke")
        print(f"Pohrana: {root}")
        print("1  Probno preuzimanje: još 25 odluka")
        print("2  Nastavi: još najviše 1.000 odluka")
        print("3  Prikaži stanje")
        print("4  Pretraži spremljene odluke")
        print("5  Zatraži pauzu drugog otvorenog HRPres prozora")
        print("0  Izlaz")
        try:
            choice = input("Odabir: ").strip()
            if choice == "0":
                return
            if choice in ("1", "2"):
                command = ["download", "--max-new", "25" if choice == "1" else "1000"]
                print("Preuzimanje je sekvencijalno. Za pauzu otvori PAUZIRAJ-HRPRES.cmd ili pritisni Ctrl+C.")
            elif choice == "3":
                command = ["status"]
            elif choice == "4":
                query = input("Riječi za lokalnu pretragu: ").strip()
                if not query:
                    continue
                command = ["search", "--query", query]
            elif choice == "5":
                command = ["pause"]
            else:
                print("Odaberi broj od 0 do 5.")
                continue
            subprocess.run([sys.executable, "-X", "utf8", "-m", "hrpres", *command, "--db", str(db)], check=False)
        except KeyboardInterrupt:
            print("\nPauza je zatražena. Spremljeni podaci ostaju dostupni.")
        except EOFError:
            return


if __name__ == "__main__":
    main()
