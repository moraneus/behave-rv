Feature: archive immutability

  Scenario: an archived task must never be edited
    Given a task is "archived"
    Then a task is "edited" never happens
