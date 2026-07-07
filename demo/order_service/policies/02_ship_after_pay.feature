Feature: fulfilment order

  Scenario: a shipment may only follow payment
    When an order is "shipped"
    Then an order is "paid" before
