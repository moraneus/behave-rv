Feature: block discipline

  Scenario: a blocked task must not complete, until unblocked
    Given a task is "blocked" until a task is "unblocked"
    Then a task is "completed" never happens
