Feature: payment safety

  Scenario: an order may only be paid after it was authorized
    When an order is "paid"
    Then an order is "authorized" before
