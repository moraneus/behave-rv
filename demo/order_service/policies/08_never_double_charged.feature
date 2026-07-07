Feature: charging integrity

  Scenario: an order is never double charged
    Then an order is "double_charged" never happens
