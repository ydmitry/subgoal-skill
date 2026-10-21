"""python3 -m subgoal: not built yet, so this only prints the planned usage."""
import sys

USAGE = """usage: python3 -m subgoal --dir DIR (--goal TEXT | --goal-file PATH) --check CMD
                         [--max-iterations N] [--max-minutes M] [--max-tokens T]
                         [--protect PATH]... [--allow RULE]... [--model NAME]
                         [--provider NAME] [--dry-run]"""

print(USAGE)
sys.exit(0 if {"-h", "--help"} & set(sys.argv[1:]) else 2)
