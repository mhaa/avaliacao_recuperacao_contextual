import os

# Agregações float de polars (mean, sum) não são bit-reprodutíveis entre
# execuções sob paralelismo — a ordem de redução varia por thread. Fixar
# single-thread aqui, antes de qualquer import que toque polars, garante o
# critério de aceitação "reexecução idêntica" (byte a byte).
os.environ.setdefault("POLARS_MAX_THREADS", "1")

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
