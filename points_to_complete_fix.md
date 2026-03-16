1. currently the change is happening for a single line item only but the details is going with the headers. the same % is not being applied for both entries which should be done. 
2. the status_id should be sent as approved i.e. 3 instead of pending or others. additionally other fields need to be updated properly and double checked for what fields are being sent and and what is not being sent 
3. Need to make the item group as well in the company in which it is being transferred i.e. the item. 
4. not showing the tracking when opening the same entry again later on. need to finalise the logic to be used to understand the serial number i.e. which one is the 1st transfer and which one is the 2nd transfer and so on.
5. need to generate the branch_mr_no when it comes back to the src_company
6. discuss what needs to be done for po
7. round off for transferring needs to be fixed.


SELECT * FROM jute_mr where jute_mr_id in (27719,27794,27795) LIMIT 100