The steps action.yml runs. Keep them mapping context onto the driver's flags; logic belongs in the
driver. Only `conclude` may fail a job, and only with an ordinary budget exit.
The real `actions/download-artifact` step runs only on `pull_request` with `saved-results-artifacts` set, and its failure is a
diagnostic. `report` posts nothing: `finish` stops the timer, judges the own commands and posts the one comment.
