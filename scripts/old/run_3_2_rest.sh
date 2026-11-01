#!/bin/bash
cd ~/llama.cpp/bench/shopagent
D=/tmp/3-2-rest.done; S=/tmp/3-2-rest.status; : > $S
while kill -0 138355 2>/dev/null; do sleep 30; done
echo "$(date +%T) clef-g12 pid 138355 exited" >> $S
go() { name=$1; shift
  [ -e runs/$name ] && { echo "$name SKIP_EXISTS" >> $S; return; }
  for i in $(seq 0 60); do u=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits|head -1); l=$(pgrep -c llama-server)
    [ "$u" -le 3000 ] && [ "$l" -eq 0 ] && break; [ $i -eq 60 ] && { echo "$name SKIP_GPU used=$u llama=$l" >> $S; return; }; sleep 30; done
  echo "$(date +%T) START $name used=$u" >> $S; s=$(date +%s)
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py "$@" > /tmp/3-2-$name.log 2>&1; rc=$?
  echo "$name rc=$rc wall=$(( $(date +%s)-s ))s" >> $S
}
go pplx-g12-r1 --config pplx-g12 --full --repeat 1
go jev-g12-r1 --config jev-g12 --full --repeat 1
go g12-native-ngl0-r1 --config g12-native --full --repeat 1 --gemma-ngl 0 --name g12-native-ngl0-r1
sleep 5; pkill -u $USER -x llama-server 2>/dev/null; sleep 2
{ cat $S; echo "llama_server_remaining=$(pgrep -c llama-server)"; date; } > $D
