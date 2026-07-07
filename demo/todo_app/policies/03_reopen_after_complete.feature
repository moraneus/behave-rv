Feature: rework discipline

  Scenario: a reopen follows a completion
    When a task is "reopened"
    Then a task is "completed" previously
