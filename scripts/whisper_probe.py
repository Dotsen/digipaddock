"""The speech recognition probe: faster-whisper small against medium on the stage CPU — kd.whisper_probe.

On the factory's host (docs/superpowers/kd9075-whisper-probe-protocol.md has the full order):

    python3 -I /opt/digipaddock/scripts/whisper_probe.py collect --card KD-<n> --out <dir>/items
    python3 -I /opt/digipaddock/scripts/whisper_probe.py vocab > <dir>/vocab.json
    <venv>/bin/python -I /opt/digipaddock/scripts/whisper_probe.py fetch --models-dir <dir>/models
    <venv>/bin/python -I /opt/digipaddock/scripts/whisper_probe.py run --items <dir>/items --out <dir>/runs \\
        --models-dir <dir>/models --vocab <dir>/vocab.json
    python3 -I /opt/digipaddock/scripts/whisper_probe.py summary <dir>/runs/runs.jsonl --items <dir>/items \\
        --phrases <phrases.json of the deployment's internal docs>
"""

import sys

sys.path.insert(0, "/opt/digipaddock/common")

from kd import whisper_probe  # noqa: E402

if __name__ == "__main__":
    sys.exit(whisper_probe.main(script=__file__))
