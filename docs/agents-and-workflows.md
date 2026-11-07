# Agents and workflows

JARVIS has several ways of working by itself. Use the smallest one that fits:

| You want | Use |
|---|---|
| A line said at a time | A reminder |
| A job done at set times | An automation |
| A list checked often, speaking only when needed | The heartbeat |
| A goal followed over days or weeks | An agent |
| Fixed steps, in a fixed order, with branches | A workflow |

## Agents

An agent is a goal JARVIS keeps working on: "watch the price of this laptop", "tell me when this project ships a new
version". It wakes every so often (every 15 minutes at the most), reads its notebook, does the next useful step, and
writes down what it needs for next time. So it carries on where it stopped, even after a restart.

It speaks only when it has something to tell you, and a quiet wake leaves nothing behind. A step that needs your yes
waits in the app. After five failures in a row an agent pauses itself, so it doesn't keep spending requests.

Create one in **Customize > Agents**, or say "keep an eye on...". No agent runs until you create it, because each wake
uses one AI request.

## Workflows

A workflow is a job laid out as steps, built in **Customize > Workflows**:

- **Ask JARVIS:** a request, with the same permissions as chat
- **Yes or no?:** a question about the last result, which picks the next step
- **Say aloud**, **Wait** and **Stop**

Each step goes to the next one unless you choose another, so a step can go back and try again. In the text, `{last}` is
the result of the step before and `{input}` is what the workflow started with. A workflow stops by itself after 100
steps, in case a loop never ends.

Start one from the screen, by saying "run the morning report workflow", or from an automation that says to run it.
