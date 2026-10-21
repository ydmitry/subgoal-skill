# subgoal

A small Python program that hands a goal to a coding agent and keeps calling it,
in a fresh session each time, until a check command that the agent does not
control says the goal holds, or until a budget ends the run.

Work in progress: the code is being built in small steps.

## Read this first: safety

- This program runs a coding agent unattended, **as you**, on your machine. Each
  call can edit any file in `--dir` and run the check command. The agent can edit
  the code the check runs, so allowing the check means it can effectively run any
  code you could run. That code sees your files, your environment variables
  (including any secrets in them) and the network.
- The loop also runs the check itself after every call. That is code the agent
  wrote, run by you.
- Work on a throwaway copy, or from a clean git state you can reset. A container
  or VM with no secrets and no network is better still.
- "Goal met" proves what the check tests, nothing more. Read the diff.

## License

MIT. A personal educational project, not affiliated with any employer or with
Anthropic.
