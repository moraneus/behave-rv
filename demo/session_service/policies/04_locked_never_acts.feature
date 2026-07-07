Feature: lockout enforcement

  Scenario: a locked user must never act
    Given a user is "locked"
    Then a user is "action" never happens
