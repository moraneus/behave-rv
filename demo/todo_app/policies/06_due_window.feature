Feature: due window

  Scenario: a started task completes within the due window
    When a task is "started"
    Then a task is "completed" within "5" seconds
