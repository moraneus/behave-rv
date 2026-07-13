Feature: assignment discipline

  Scenario: a ticket may only be resolved after it was assigned
    When a ticket is "resolved"
    Then a ticket is "assigned" before
