Feature: escalation handling

  Scenario: an escalated ticket must not be closed until resolved
    Given a ticket is "escalated" until a ticket is "resolved"
    Then a ticket is "closed" never happens
