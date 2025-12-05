# schoology-ics

### Features

A simple Flask server that organizes your Schoology calendar feed.
It stacks events and assignments per day, producing an organized list of
assignments and events per day on your calendar.

Additional features include:
 - Schoology assignment submission checking
 - Marking assignments as done
 - Adding custom events and assignments

### Running

To run, import packages from the `uv` package manager and run `main.py`.

On Mac, you can use `launch.sh` to run in the background, where it will remain
active until restarted.

Provide environment variables `SCHOOLOGY_KEY`, `SCHOOLOGY_SECRET`, and
`SCHOOLOGY_UID` to access Schoology API.