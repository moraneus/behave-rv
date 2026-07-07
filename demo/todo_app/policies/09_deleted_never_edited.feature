Feature: deletion integrity

  Scenario: a deleted task must never be edited
    Given a task is "deleted"
    Then a task is "edited" never happens
