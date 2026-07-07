Feature: logout enforcement

  Scenario: a logged-out user must never act
    Given a user is "logout"
    Then a user is "action" never happens
