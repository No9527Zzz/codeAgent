package example;

import org.junit.Test;
import static org.junit.Assert.*;

public class OrderServiceTest {
    @Test
    public void closesAtTimeoutBoundary() {
        assertTrue(new OrderService().shouldClose(30));
    }

    @Test
    public void keepsUnexpiredOrderOpen() {
        assertFalse(new OrderService().shouldClose(29));
    }
}
