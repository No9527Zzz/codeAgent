package example;

public class OrderService {
    public boolean shouldClose(int unpaidMinutes) {
        return unpaidMinutes >= 30;
    }
}
