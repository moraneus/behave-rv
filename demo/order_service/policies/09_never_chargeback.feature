Feature: chargeback watch

  Scenario: an order is never charged back
    Then an order is "chargeback" never happens
