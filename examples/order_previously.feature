Feature: immediate authorization

  Scenario: the event immediately before payment must be authorization
    When an order is "paid"
    Then an order is "authorized" previously
