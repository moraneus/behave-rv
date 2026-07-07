Feature: task lifecycle order

  Scenario: a task may only be completed after it was started
    When a task is "completed"
    Then a task is "started" before
